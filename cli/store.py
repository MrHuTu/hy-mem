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
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


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
    """
    while True:
        row = conn.execute(
            f"SELECT * FROM jobs WHERE status='pending' AND kind IN ({','.join('?' * len(kinds))}) "
            "ORDER BY id LIMIT 1",
            kinds,
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
    conn.execute(
        "UPDATE jobs SET status='done', finished_at=?, memory_ids=?, error=NULL WHERE id=?",
        (time.time(), json.dumps(memory_ids, ensure_ascii=False), job_id),
    )


def mark_failed(conn, job_id: int, error: str, retry: bool) -> str:
    row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    attempts, max_attempts = (row["attempts"], row["max_attempts"]) if row else (0, 0)
    if retry and attempts < max_attempts:
        conn.execute("UPDATE jobs SET status='pending', error=? WHERE id=?", (error[:500], job_id))
        return "pending"
    conn.execute(
        "UPDATE jobs SET status='dead', finished_at=?, error=? WHERE id=?", (time.time(), error[:500], job_id)
    )
    return "dead"


def requeue_stale(conn, older_than: float | None = None) -> int:
    """把崩溃 worker 留下的 running 任务放回 pending。"""
    if older_than is None:
        older_than = float(os.getenv("HY_MEM_STALE_SEC") or 900)
    cutoff = time.time() - older_than
    cur = conn.execute(
        "UPDATE jobs SET status='pending', started_at=NULL, worker=NULL "
        "WHERE status='running' AND COALESCE(started_at, 0) < ?",
        (cutoff,),
    )
    return cur.rowcount


def queue_stats(conn) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    stats = {r["status"]: r["n"] for r in rows}
    oldest = conn.execute("SELECT MIN(created_at) t FROM jobs WHERE status='pending'").fetchone()
    return {
        "total": sum(stats.values()),
        "pending": stats.get("pending", 0),
        "running": stats.get("running", 0),
        "done": stats.get("done", 0),
        "dead": stats.get("dead", 0),
        "oldest_pending_age_s": round(time.time() - oldest["t"], 1) if oldest and oldest["t"] else None,
    }


def list_jobs(conn, status: str | None = None, limit: int = 20) -> list[dict]:
    if status:
        rows = conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:  # noqa: BLE001
            pass
        try:
            d["memory_ids"] = json.loads(d["memory_ids"]) if d.get("memory_ids") else []
        except Exception:  # noqa: BLE001
            pass
        out.append(d)
    return out


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
