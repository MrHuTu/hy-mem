"""hy-mem 的本地状态层：异步任务队列 + 标签索引 + 跨进程写锁。

三个组件都落在一个 SQLite 文件里（`MEMORY_DATA_DIR/hy_mem_queue.db`），
理由：
  1. 队列要能"进程退出后任务还在"，纯内存队列不满足；SQLite 单文件、零服务、可备份。
  2. 标签需要按 memory_id 反查，SQLite 的二级索引足够，不必引入额外存储。
  3. 写锁必须跨进程生效，且进程崩溃后要自动释放 —— 用 OS 级的字节锁
     （Windows msvcrt / POSIX fcntl），而不是"创建锁文件"，因为后者留残锁。

为什么要写锁：Hy-Memory SDK 内部**没有任何文件锁**（已核对 filelock/flock/fcntl
均无引用），而它的 reconcile 是"读旧节点 → 比较 → 覆盖写"的读改写序列。
两个进程同时写同一批节点时会丢更新。SDK 不管，只能在外壳层串行化。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def data_dir() -> Path:
    d = Path(os.getenv("MEMORY_DATA_DIR") or (Path.home() / ".hy_memory"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "hy_mem_queue.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL DEFAULT 'add',
    payload     TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    mode        TEXT,
    error       TEXT,
    memory_ids  TEXT,
    created_at  REAL    NOT NULL,
    started_at  REAL,
    finished_at REAL,
    not_before  REAL,
    worker      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);

CREATE TABLE IF NOT EXISTS tags (
    memory_id  TEXT NOT NULL,
    tag        TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL,
    PRIMARY KEY (memory_id, tag, source)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS idx_tags_mid ON tags(memory_id);

CREATE TABLE IF NOT EXISTS worker_hb (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    ts     REAL NOT NULL,
    worker TEXT
);

CREATE TABLE IF NOT EXISTS worker_reg (
    id  TEXT PRIMARY KEY,
    ts  REAL NOT NULL,
    pid INTEGER
);
"""


def _migrate(conn) -> None:
    """旧库幂等迁移。CREATE TABLE IF NOT EXISTS 不会给已有表补列，这里补。

    0.9.8：jobs.not_before（延迟重试的最早可领时间）。并发迁移的竞态用
    "duplicate column" 当成功处理——两个进程同时 ALTER，后到者报这个错，
    说明列已经在了，目的已达成。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "not_before" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN not_before REAL")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def connect() -> sqlite3.Connection:
    # 并发启动（多子代理）下的已知瞬态：另一进程 checkpoint/重建 WAL 的窗口里，
    # 新连接可能拿到只读句柄，PRAGMA 阶段就报 "attempt to write a readonly
    # database"（并发压测实测约 5%）。锁类错误 busy_timeout 会自己等，这种
    # 不等——短退避重开连接即可，重试后必拿正常句柄。
    last: Exception | None = None
    for attempt in range(4):
        conn = sqlite3.connect(str(db_path()), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executescript(SCHEMA)
            _migrate(conn)
            return conn
        except sqlite3.OperationalError as e:
            conn.close()
            if "readonly" not in str(e).lower() and "locked" not in str(e).lower():
                raise
            last = e
            time.sleep(0.15 * (attempt + 1))
    raise last  # type: ignore[misc]


def transient_write(fn, attempts: int = 4):
    """执行一轮写操作，readonly/locked 瞬态自动换新连接重试。

    WAL 在「最后一个连接关闭 → 删 -wal/-shm → 下个连接重建」的窗口里，
    别人已打开的连接写入会报 attempt to write a readonly database（Windows
    并发实测约 5%）——连接被污染只能换新的，锁类错误也顺路在这里重试。
    fn(conn) -> 结果；fn 只做写，读放外面。
    """
    last: Exception | None = None
    for i in range(attempts):
        conn = connect()
        try:
            r = fn(conn)
            conn.close()
            return r
        except sqlite3.OperationalError as e:
            conn.close()
            s = str(e).lower()
            if not ("readonly" in s or "locked" in s or "busy" in s):
                raise
            last = e
            time.sleep(0.15 * (i + 1))
    raise last  # type: ignore[misc]


# --------------------------------------------------------------------------
# 跨进程写锁
# --------------------------------------------------------------------------
class WriteLock:
    """排他写锁。进程异常退出时由 OS 释放，不会留残锁。

    用法：
        with WriteLock() as lk:
            lk.acquired  # True=真拿到锁；False=超时但仍放行（降级）
    """

    def __init__(self, timeout: float | None = None, name: str = "write.lock", strict: bool = True):
        if timeout is None:
            timeout = float(os.getenv("HY_MEM_LOCK_TIMEOUT") or 180)
        self.path = data_dir() / name
        self.timeout = timeout
        self.strict = strict
        self.acquired = False
        self._fh = None

    def _try_lock(self) -> bool:
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def __enter__(self):
        deadline = time.time() + self.timeout
        warned = False
        while True:
            if self._try_lock():
                self.acquired = True
                try:
                    self._fh.seek(0)
                    self._fh.truncate()
                    self._fh.write(f"{os.getpid()} {time.time():.0f}".encode())
                    self._fh.flush()
                except OSError:
                    pass
                return self
            if time.time() >= deadline:
                if self.strict:
                    raise TimeoutError(
                        f"另一进程正在写入记忆库，等待 {self.timeout:.0f}s 后仍未拿到锁。"
                        f"（锁文件 {self.path}）可用 HY_MEM_LOCK_TIMEOUT 调大，或先跑 hy-mem queue 看是否有 worker 卡住"
                    )
                return self
            if not warned and time.time() > deadline - self.timeout + 2:
                print(f"hy-mem: 等待写锁（另一进程正在写入）…", file=sys.stderr)
                warned = True
            time.sleep(0.15)

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                if self.acquired:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            self._fh.seek(0)
                            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
            finally:
                self._fh.close()
                self._fh = None
        return False


# --------------------------------------------------------------------------
# 队列
# --------------------------------------------------------------------------
def enqueue(conn, payload: dict, kind: str = "add", mode: str | None = None, max_attempts: int = 3) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (kind, payload, status, max_attempts, mode, created_at) VALUES (?,?,?,?,?,?)",
        (kind, json.dumps(payload, ensure_ascii=False), "pending", max_attempts, mode, time.time()),
    )
    return int(cur.lastrowid)


def claim_next(conn, worker_id: str, kinds: tuple[str, ...] = ("add", "finalize")):
    """原子领取一个待处理任务。返回 row 或 None。

    UPDATE ... WHERE status='pending' 的 rowcount 就是抢占是否成功，
    因此多个 worker 同时启动也不会重复消费同一条。

    not_before：延迟重试的最早可领时间（失败退避写在这里，Celery 的
    ETA/countdown 同款模式）。未到期的任务对 worker 不存在，claim 直接
    跳过——失败任务的退避等待不阻塞队列里的其它任务。
    """
    while True:
        row = conn.execute(
            f"SELECT * FROM jobs WHERE status='pending' AND kind IN ({','.join('?' * len(kinds))}) "
            "AND (not_before IS NULL OR not_before <= ?) "
            "ORDER BY id LIMIT 1",
            (*kinds, time.time()),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE jobs SET status='running', worker=?, started_at=?, attempts=attempts+1 "
            "WHERE id=? AND status='pending'",
            (worker_id, time.time(), row["id"]),
        )
        if cur.rowcount == 1:
            return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def mark_done(conn, job_id: int, memory_ids: list[str], extra: dict | None = None) -> None:
    """任务成功落账。extra 里的 note 写进 error 列当告警留痕。

    为什么把 note 塞进 error 列（0.9.10 / PRD F9）：jobs 表没有独立备注列，
    迁移一个新列不划算，而 error 在 done 行上本来就是空的。留痕的价值是
    「写进去了但检索不到」不再静默——`queue --status done` 能看见
    shadow_only，也能用 `retry --shadow` 一键重做。
    """
    note = (extra or {}).get("note")
    conn.execute(
        "UPDATE jobs SET status='done', finished_at=?, memory_ids=?, error=? WHERE id=?",
        (time.time(), json.dumps(memory_ids, ensure_ascii=False),
         (str(note)[:500] if note else None), job_id),
    )


def requeue_shadow(conn) -> int:
    """把「只落了 l1_raw 影子节点」的完成任务重投（PRD F9 的自愈入口）。

    这些任务当时算成功（抽取没报错，只是没产出可检索节点），但记忆实际
    搜不到。重投会再跑一次抽取，让用户可以一键补救，而不是手工 re-add。
    """
    cur = conn.execute(
        "UPDATE jobs SET status='pending', attempts=0, error=NULL, started_at=NULL, "
        "worker=NULL, not_before=NULL WHERE status='done' AND error LIKE 'shadow_only%'"
    )
    return cur.rowcount


def mark_failed(conn, job_id: int, error: str, retry: bool) -> str:
    """失败落账。retry=True 且还有重试预算时回到 pending，并写退避 not_before。

    退避曲线 min(2**attempts, 30)（attempts 已在 claim 时 +1）：
    第 1/2/3 次失败分别延迟 2/4/8s，上限 30s。延迟写在任务行上而不是
    worker 循环里 sleep——单消费者时代一个失败任务的退避会把整个队列
    卡住最长 30s（head-of-line blocking），现在失败任务只是"暂不可领"。
    """
    row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    attempts, max_attempts = (row["attempts"], row["max_attempts"]) if row else (0, 0)
    if retry and attempts < max_attempts:
        delay = min(2 ** int(attempts or 1), 30)
        conn.execute(
            "UPDATE jobs SET status='pending', error=?, not_before=? WHERE id=?",
            (error[:500], time.time() + delay, job_id),
        )
        return "pending"
    conn.execute(
        "UPDATE jobs SET status='dead', finished_at=?, error=? WHERE id=?", (time.time(), error[:500], job_id)
    )
    return "dead"


def requeue_stale(conn, older_than: float | None = None) -> int:
    """把崩溃 worker 留下的 running 任务放回 pending（清掉 not_before：重投立即到期）。"""
    if older_than is None:
        older_than = float(os.getenv("HY_MEM_STALE_SEC") or 900)
    cutoff = time.time() - older_than
    cur = conn.execute(
        "UPDATE jobs SET status='pending', started_at=NULL, worker=NULL, not_before=NULL "
        "WHERE status='running' AND COALESCE(started_at, 0) < ?",
        (cutoff,),
    )
    return cur.rowcount


def queue_stats(conn) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    stats = {r["status"]: r["n"] for r in rows}
    oldest = conn.execute("SELECT MIN(created_at) t FROM jobs WHERE status='pending'").fetchone()
    delayed = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE status='pending' AND not_before > ?", (time.time(),)
    ).fetchone()
    shadow = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE status='done' AND error LIKE 'shadow_only%'"
    ).fetchone()
    return {
        "total": sum(stats.values()),
        "pending": stats.get("pending", 0),
        "running": stats.get("running", 0),
        "done": stats.get("done", 0),
        "dead": stats.get("dead", 0),
        "delayed": int(delayed["n"] or 0),
        "shadow_only": int(shadow["n"] or 0),
        "oldest_pending_age_s": round(time.time() - oldest["t"], 1) if oldest and oldest["t"] else None,
    }


# --------------------------------------------------------------------------
# worker 心跳/注册（add --async 判断要不要自动拉起临时 worker、以及
# worker 数量上限，都用这张表）
# --------------------------------------------------------------------------
def heartbeat(conn, worker_id: str) -> None:
    """worker 每轮循环调用：按 worker 注册行 UPSERT，开销可忽略。

    单行 worker_hb（历史）升级为按 worker 一行的 worker_reg——同一张表
    同时回答「最近的 worker 活着吗」（worker_age）和「活着几个」（count）。
    """
    import os as _os

    conn.execute(
        "INSERT INTO worker_reg (id, ts, pid) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, pid=excluded.pid",
        (worker_id, time.time(), _os.getpid()),
    )


def worker_age(conn) -> float | None:
    """距最近一次 worker 心跳的秒数；从无心跳返回 None。"""
    row = conn.execute("SELECT MAX(ts) AS ts FROM worker_reg").fetchone()
    if row is None or not row["ts"]:
        return None
    return max(0.0, time.time() - row["ts"])


def count_live_workers(conn, fresh: float = 15.0) -> int:
    """心跳在 fresh 秒内的 worker 数——拉起闸门的上限判断用。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM worker_reg WHERE ts > ?", (time.time() - fresh,)
    ).fetchone()
    return int(row["n"] or 0)


def worker_leave(conn, worker_id: str) -> None:
    """worker 退出时注销自己；失败不影响退出流程。"""
    try:
        conn.execute("DELETE FROM worker_reg WHERE id = ?", (worker_id,))
    except sqlite3.OperationalError:  # noqa: BLE001
        pass


def worker_purge_stale(conn, older_than: float = 300.0) -> None:
    """清理硬杀残留的注册行（心跳停更超过 older_than 秒）。"""
    try:
        conn.execute("DELETE FROM worker_reg WHERE ts < ?", (time.time() - older_than,))
    except sqlite3.OperationalError:  # noqa: BLE001
        pass


def claim_worker_slot(conn, cap: int, fresh: float = 15.0) -> bool:
    """拉起 worker 前的占位闸门——必须在 WriteLock 内调用（串行化竞态）。

    清理过期行 → 数存活 worker → 未达 cap 则写一行 '_spawn_intent' 占位。
    占位行 15s 后自然过期（真实 worker 首次心跳 ~2s 就会顶上自己的行），
    即使 Popen 失败也只占 15s 名额，不会永久卡死拉起。

    额外清理「pid 已经不存在的登记行」（0.9.10 / PRD F10）：被 SIGKILL 的
    worker 不再心跳，但它的行要等到 fresh 秒后才过期，这段时间里所有拉起
    请求都被闸门挡掉，检索只能干等端点超时。按 pid 探活可以立即腾出名额。
    只对 ts 已超过 3s 的行做探活——给刚启动、还没来得及记心跳的进程留余量。
    """
    try:
        cutoff = time.time() - fresh
        conn.execute("DELETE FROM worker_reg WHERE ts < ?", (cutoff,))
        _purge_dead_pids(conn, older_than=3.0)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM worker_reg WHERE ts > ?", (cutoff,)
        ).fetchone()
        if int(n["n"] or 0) >= cap:
            return False
        conn.execute(
            "INSERT INTO worker_reg (id, ts, pid) VALUES ('_spawn_intent', ?, NULL) "
            "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts",
            (time.time(),),
        )
        return True
    except sqlite3.OperationalError:  # noqa: BLE001  闸门自身故障宁可多拉
        return True


def pid_alive(pid) -> bool:
    """进程是否还在。与 vdb_endpoint.pid_alive 同样的实现，但放在 store 里
    避免「闸门 → 端点模块 → store」的循环导入。"""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ACCESS_DENIED = 进程存在
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _purge_dead_pids(conn, older_than: float = 3.0) -> int:
    """删掉「pid 已死」的注册行，返回删除条数。"""
    try:
        rows = conn.execute(
            "SELECT id, pid, ts FROM worker_reg WHERE pid IS NOT NULL AND ts < ?",
            (time.time() - older_than,),
        ).fetchall()
    except sqlite3.OperationalError:  # noqa: BLE001
        return 0
    dead = [r["id"] for r in rows if not pid_alive(r["pid"])]
    for wid in dead:
        try:
            conn.execute("DELETE FROM worker_reg WHERE id = ?", (wid,))
        except sqlite3.OperationalError:  # noqa: BLE001
            pass
    return len(dead)


def _job_row(r) -> dict:
    d = dict(r)
    try:
        d["payload"] = json.loads(d["payload"])
    except Exception:  # noqa: BLE001
        pass
    try:
        d["memory_ids"] = json.loads(d["memory_ids"]) if d.get("memory_ids") else []
    except Exception:  # noqa: BLE001
        pass
    return d


def list_jobs(conn, status: str | None = None, limit: int = 20) -> list[dict]:
    if status:
        rows = conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_job_row(r) for r in rows]


def get_jobs(conn, ids: list[int]) -> list[dict]:
    """按 job_id 精确取（MCP queue_status 用：智能体只关心自己刚提交的那几条）。"""
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE id IN ({marks}) ORDER BY id", ids
    ).fetchall()
    return [_job_row(r) for r in rows]


# --------------------------------------------------------------------------
# 标签
# --------------------------------------------------------------------------
def bind_tags(conn, memory_ids: list[str], tags: list[str], source: str = "user") -> int:
    now = time.time()
    n = 0
    for mid in memory_ids:
        for t in tags:
            t = (t or "").strip()
            if not t:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO tags (memory_id, tag, source, created_at) VALUES (?,?,?,?)",
                (mid, t, source, now),
            )
            n += cur.rowcount
    return n


def tags_for(conn, memory_ids: list[str]) -> dict[str, list[str]]:
    """批量取本地标签，避免 N+1 查询。"""
    if not memory_ids:
        return {}
    marks = ",".join("?" * len(memory_ids))
    rows = conn.execute(
        f"SELECT memory_id, tag, source FROM tags WHERE memory_id IN ({marks})", list(memory_ids)
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["memory_id"], [])
        if r["tag"] not in out[r["memory_id"]]:
            out[r["memory_id"]].append(r["tag"])
    return out


def untag(conn, memory_id: str | None, tag: str | None = None) -> int:
    if memory_id and tag:
        cur = conn.execute("DELETE FROM tags WHERE memory_id=? AND tag=?", (memory_id, tag))
    elif memory_id:
        cur = conn.execute("DELETE FROM tags WHERE memory_id=?", (memory_id,))
    elif tag:
        cur = conn.execute("DELETE FROM tags WHERE tag=?", (tag,))
    else:
        cur = conn.execute("DELETE FROM tags")
    return cur.rowcount


def tag_inventory(conn, min_count: int = 1) -> list[dict]:
    rows = conn.execute(
        "SELECT tag, "
        "  SUM(CASE WHEN source='user' THEN 1 ELSE 0 END) user_n, "
        "  SUM(CASE WHEN source='auto' THEN 1 ELSE 0 END) auto_n, "
        "  COUNT(*) n "
        "FROM tags GROUP BY tag HAVING n >= ? ORDER BY n DESC, tag ASC",
        (min_count,),
    ).fetchall()
    return [dict(r) for r in rows]


def prune(conn, alive_ids: set[str] | None = None) -> int:
    """清掉指向已删记忆的标签行；alive_ids=None 时只清明显失效的自动标签。"""
    if alive_ids is None:
        return 0
    rows = conn.execute("SELECT DISTINCT memory_id FROM tags").fetchall()
    dead = [r["memory_id"] for r in rows if r["memory_id"] not in alive_ids]
    n = 0
    for mid in dead:
        n += conn.execute("DELETE FROM tags WHERE memory_id=?", (mid,)).rowcount
    return n
