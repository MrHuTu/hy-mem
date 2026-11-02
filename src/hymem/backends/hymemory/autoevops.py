# -*- coding: utf-8 -*-
"""L6 自动沉淀：事件驱动（add 后顺带检查），无定时线程。

设计（docs/specs/2026-10-30-evolve-auto/，用户拍板 2026-10-30）
--------------------------------------------------------------
* 不再暴露 evolve 工具给模型——手动调没人记得，SDK 的增量 checkpoint 机制
  （``s2_reviewed_source_count``）天然支持「素材攒够自动跑、重复跑不重算、
  无簇零成本退出（no_clusters 不调 LLM）」。
* MCP 活跃时（``_tool_add_memory`` 完成后）顺带调 :func:`maybe_auto_evolve`，
  检查是微秒级（读小状态文件 + 时间戳比对），满足条件才起一次性 daemon 线程。

并发模型（重要，两层防线）
--------------------------
1. 同进程：``_RUN_LOCK``（threading.Lock 非阻塞 try-acquire）。拿不到说明
   沉淀正在跑 → 立即放弃（用户语义："发现正在有 L6 沉淀，应该立马结束"）。
   MCP 主循环本就逐条消息串行处理，同进程唯一会与图库并发的就是这个线程。
2. 跨进程：kuzu 文件锁是 OS 级排他锁——另一个 MCP 进程的沉淀线程在
   ``build client`` 时直接抛 ``Could not set lock``，被捕获记 ``skipped_lock``，
   零副作用（实测多次：三会话抢锁、CLI doctor 被拒、read_only 被拒全是它）。
   不需要应用层跨进程锁——kuzu 锁已经是内核级互斥，再加只是把同样的失败
   提前几毫秒。

状态与退避（R5）
----------------
* 状态文件：``data_dir()/evolve_jobs.json``（MEMORY_DATA_DIR 下，多进程共享）。
  写入一律「临时文件 + os.replace 原子替换」——多进程同时触发的
  read-modify-write 交错可能丢一条记录（可接受），JSON 撕裂不可接受（消灭）。
* 冷却：距上次**尝试**（成功/失败/素材不足都算）≥ cooldownHours × 2^连续失败数
  （上限 7 天），成功清零。从「尝试时刻」起算是故意的：防止冷却期内每条 add
  都去做全库计数。
* 阈值：自上次成功沉淀以来新增 L2 fact 数 ≥ minNewFacts 才真跑。SDK 的
  no_clusters 让试跑零成本，阈值只是防孤簇白烧 LLM。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

# 同进程互斥（R4）：同一 MCP 进程内同时只允许一个沉淀线程。
_RUN_LOCK = threading.Lock()

# 状态文件最多保留多少条记录（防无限膨胀）。
_MAX_RECORDS = 200

# 连续失败退避上限：cooldownHours × 2^8 ≈ 256 倍，7 天封顶由 _backoff 裁剪。
_BACKOFF_CAP_EXP = 8
_BACKOFF_CAP_HOURS = 24 * 7

# 记录的 status 取值：
#   success             —— evolve 跑完（含 schemas_added=0 的 no_clusters，零成本）
#   failed              —— evolve 抛非锁异常（网关断、SDK 错）
#   skipped_small       —— 冷却过了但素材增量 < minNewFacts
#   skipped_lock        —— 跨进程撞 kuzu 锁（另一 MCP 进程持有图库）
#   skipped_error       —— build client 等前置失败（按 failed 计退避）
# skipped_concurrent（同进程锁被占）不落盘：另一线程跑完自然会写，避免并发写。


def _state_path() -> Path:
    from hymem.core.store import data_dir

    return data_dir() / "evolve_jobs.json"


def load_state() -> dict:
    """读状态文件。损坏/缺失按首次运行处理（基线 0，无记录）。"""
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("records", [])
            raw.setdefault("last_total_facts", 0)
            raw.setdefault("last_attempt_ts", 0.0)
            raw.setdefault("consecutive_failures", 0)
            return raw
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 —— 损坏的 JSON 按「没有历史」处理
        pass
    return {"records": [], "last_total_facts": 0, "last_attempt_ts": 0.0,
            "consecutive_failures": 0}


def _save_state(st: dict) -> None:
    """原子写（临时文件 + os.replace）。多进程共享文件，绝不能写一半。"""
    import os

    p = _state_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _auto_cfg(cfg: dict | None = None) -> dict:
    """取 evolve.auto 三项（缺省回落 DEFAULTS）。"""
    from hymem.core import config as cfgmod

    cfg = cfg if cfg is not None else cfgmod.load()
    ev = cfg.get("evolve") or {}
    auto = ev.get("auto") or {}
    d = (cfgmod.DEFAULTS.get("evolve") or {}).get("auto") or {}
    return {
        "enabled": bool(auto.get("enabled", d.get("enabled", True))),
        "minNewFacts": int(auto.get("minNewFacts") or d.get("minNewFacts") or 8),
        "cooldownHours": float(auto.get("cooldownHours") or d.get("cooldownHours") or 24),
    }


def _backoff_hours(consecutive_failures: int, base_hours: float) -> float:
    """连续失败 n 次 → 冷却 ×2^n，7 天封顶。"""
    if consecutive_failures <= 0:
        return base_hours
    exp = min(consecutive_failures, _BACKOFF_CAP_EXP)
    return min(base_hours * (2 ** exp), _BACKOFF_CAP_HOURS)


def should_attempt(st: dict, auto: dict, now: float | None = None) -> tuple[bool, str]:
    """冷却期判断（纯内存运算，微秒级）。返回 (是否可尝试, 不可尝试原因)。"""
    now = time.time() if now is None else now
    cd = _backoff_hours(int(st.get("consecutive_failures") or 0),
                        float(auto["cooldownHours"]))
    due = float(st.get("last_attempt_ts") or 0.0) + cd * 3600
    if now < due:
        return False, "cooldown"
    return True, ""


def _fact_count(client, user_id: str) -> int:
    """数当前身份下 ACTIVE 的 L2 fact 总数（失败返回 -1，跳过本轮阈值判断）。"""
    import asyncio

    from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus

    try:
        gs = client._graph_store
        iso = MemoryNode.build_isolation_key(user_id, "default_agent")
        rows = asyncio.run(
            gs.get_all_nodes(
                isolation_key=iso,
                layer=MemoryLayer.L2_FACT,
                status=MemoryStatus.ACTIVE,
                limit=100000,
            )
        )
        return len(rows or [])
    except Exception:  # noqa: BLE001
        return -1


def _record(st: dict, status: str, **extra) -> None:
    """追加一条记录（含时间戳），截断到上限。调用方负责 _save_state。"""
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "status": status}
    rec.update(extra)
    st.setdefault("records", []).append(rec)
    if len(st["records"]) > _MAX_RECORDS:
        st["records"] = st["records"][-_MAX_RECORDS:]


def _run_auto(user_id: str, auto: dict) -> None:
    """沉淀线程体（一次性 daemon）。调用方已持有 _RUN_LOCK，结束时释放。"""
    try:
        st = load_state()
        from hymem.core.evolveops import (
            build_evolve_client,
            evolve,
            kuzu_lock_error,
        )

        # ① 独立 client（跨进程撞 kuzu 锁在这里爆出来）
        try:
            client = build_evolve_client()
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            lock = kuzu_lock_error(msg)
            if lock:
                _record(st, "skipped_lock", error=msg[:200])
                st["last_attempt_ts"] = time.time()  # 尝试即计时（防每条 add 撞一次）
            else:
                st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
                _record(st, "skipped_error", error=msg[:200])
                st["last_attempt_ts"] = time.time()
            _save_state(st)
            return

        try:
            # ② 素材增量阈值
            total = _fact_count(client, user_id)
            base = int(st.get("last_total_facts") or 0)
            if total >= 0:
                new_facts = max(0, total - base)
                if new_facts < int(auto["minNewFacts"]):
                    _record(st, "skipped_small", new_facts=new_facts,
                            total=total, threshold=int(auto["minNewFacts"]))
                    st["last_attempt_ts"] = time.time()
                    _save_state(st)
                    return

            # ③ 真跑（语言指令在 evolveops.evolve 内按 evolve.language 注入）
            res = evolve(client=client, user_id=user_id)
            if res.get("ok"):
                st["consecutive_failures"] = 0
                st["last_total_facts"] = total if total >= 0 else base
                _record(st, "success", schemas_added=int(res.get("schemas_added") or 0),
                        batches=int(res.get("batches_completed") or 0),
                        idle=bool(res.get("idle")),
                        language=res.get("language") or "")
            else:
                msg = str(res.get("error") or "")
                if kuzu_lock_error(msg):
                    _record(st, "skipped_lock", error=msg[:200])
                else:
                    st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
                    _record(st, "failed", error=msg[:200])
            st["last_attempt_ts"] = time.time()
            _save_state(st)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001 —— 线程体绝不向外抛
        try:
            st = load_state()
            st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
            _record(st, "failed", error=f"{type(e).__name__}: {e}"[:200])
            st["last_attempt_ts"] = time.time()
            _save_state(st)
        except Exception:  # noqa: BLE001
            pass
    finally:
        _RUN_LOCK.release()


def maybe_auto_evolve() -> dict:
    """add 等工具调用完成后顺带调用。非阻塞、绝不抛异常、微秒级返回。

    返回（仅诊断用，add 响应不携带）：
      {"triggered": True}                          —— 已起沉淀线程（本调用持有 _RUN_LOCK）
      {"skipped": "mode|disabled|cooldown|lock"}   —— 没触发及原因
    """
    try:
        from hymem.core import config as cfgmod

        cfg = cfgmod.load()
        if cfg.get("mode") != "ultra":
            return {"skipped": "mode"}
        auto = _auto_cfg(cfg)
        if not auto["enabled"]:
            return {"skipped": "disabled"}
        st = load_state()
        ok, _reason = should_attempt(st, auto)
        if not ok:
            return {"skipped": "cooldown"}
        # 锁在这里获取并移交给线程（MCP 主循环串行处理消息，不存在两个
        # maybe 并发抢锁；线程调度延迟不会造成双线程——拿不到就是有人在跑）。
        if not _RUN_LOCK.acquire(blocking=False):
            return {"skipped": "lock"}  # 同进程已有沉淀在跑：立即结束（R4）
        user_id = cfgmod.identity_defaults(cfg)
        threading.Thread(
            target=_run_auto, args=(user_id, auto),
            name="hymem-auto-evolve", daemon=True,
        ).start()
        return {"triggered": True}
    except Exception as e:  # noqa: BLE001 —— 挂在 add 尾部，绝不影响 add
        return {"error": f"{type(e).__name__}: {e}"[:120]}


def status_summary(limit: int = 20) -> dict:
    """evolve_status 工具的数据源：最近记录 + 汇总。"""
    from hymem.core import config as cfgmod

    st = load_state()
    auto = _auto_cfg()
    ok, _ = should_attempt(st, auto)
    cd = _backoff_hours(int(st.get("consecutive_failures") or 0),
                        float(auto["cooldownHours"]))
    due = float(st.get("last_attempt_ts") or 0.0) + cd * 3600
    recs = list(st.get("records") or [])[-max(0, min(limit, 100)):]
    last_ok = next((r for r in reversed(st.get("records") or [])
                    if r.get("status") == "success"), None)
    return {
        "auto": auto,
        "mode": (cfgmod.load().get("mode")),
        "consecutive_failures": int(st.get("consecutive_failures") or 0),
        "last_total_facts": int(st.get("last_total_facts") or 0),
        "last_success": (last_ok or {}).get("ts", ""),
        "next_attempt_after": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(due))
        if st.get("last_attempt_ts") else "",
        "can_attempt_now": ok,
        "records": recs,
    }
