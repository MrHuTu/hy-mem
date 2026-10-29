# -*- coding: utf-8 -*-
"""

S3-1（2026-10-29）：本模块从入口 cli.py 平移而来（纯搬家，逻辑零变化）。
依赖方向：core 内部互引 + stdlib/SDK；不依赖 ui/mcp/入口。

"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hymem.core import install_ops
from hymem.core import store


def data_dir() -> Path:
    p = Path(os.getenv("MEMORY_DATA_DIR") or (Path.home() / ".hy_memory"))
    return p


def _worker_cap() -> int:
    """消费并发线程数（lane 数）。HY_MEM_MAX_WORKERS 可覆盖，默认 3。

    0.9.8 起语义变化：以前是「detached worker 进程数上限」，现在是单个
    supervisor 进程内的消费线程数。SDK 的 reconcile 是读改写序列，跨进程/
    跨线程必须由 WriteLock 串行（锁覆盖整个 client.add 含 LLM 抽取），
    所以多个消费进程是伪并发——N 个进程只会在锁上排队，还各付一份 SDK
    初始化（hy_memory.log / chroma 的多进程初始化竞态正是历史并发风暴的
    根源）。线程池里锁语义不变，非临界区（snapshot 读、入账、退避调度）
    可与在途写入重叠。
    """
    try:
        return max(1, int(os.getenv("HY_MEM_MAX_WORKERS") or 3))
    except ValueError:
        return 3
def _spawn_detached_worker(idle_exit: float = 90.0,
                           extra_args: list[str] | None = None) -> bool:
    """拉起一个脱管的后台消费 supervisor（空闲自退出），成功返回 True。

    为什么不常驻：智能体场景下「临时拉起 + 空闲自杀」最干净 —— add --async
    入队后立即返回毫秒级，落库由后台进程完成，不留僵尸进程。

    消费并发模型（0.9.8）：detached supervisor 恒为 1 个进程，进程内跑
    N 个消费线程（--lanes，默认 HY_MEM_MAX_WORKERS=3）。进程数恒 1 是
    有意的——见 _worker_cap 的语义说明。WriteLock 与 SQLite 抢占兜底，
    即使拉重了也无害（闸门 cap=1 + 心跳判活）。
    """
    from hymem.core import install_ops

    # 上限闸门（WriteLock 内占位，串行化风暴竞态）：detached supervisor 恒 1 个。
    # 占位行 15s 过期，Popen 失败也不会永久卡死后续拉起。
    try:
        with store.WriteLock():
            conn = store.connect()
            try:
                slot = store.claim_worker_slot(conn, 1)
            finally:
                conn.close()
    except Exception:  # noqa: BLE001  闸门失败宁可多拉不多丢
        slot = True
    if not slot:
        return False

    argv = install_ops.target_command() + [
        "worker",
        "--idle-exit", str(idle_exit),
        "--poll", "1.0",
        "--lanes", str(_worker_cap()),
    ] + (extra_args or [])
    try:
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW：不闪黑框
            # stderr 落文件而非 DEVNULL：worker 若在启动期静默死亡（日志
            # 文件竞态/readonly 等），这里就是唯一现场
            try:
                err_f = open(store.data_dir() / "logs" / "worker.err", "ab")
            except OSError:
                err_f = subprocess.DEVNULL
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f, creationflags=flags, close_fds=True,
            )
        else:
            err_f = None
            try:
                (store.data_dir() / "logs").mkdir(parents=True, exist_ok=True)
                err_f = open(store.data_dir() / "logs" / "worker.err", "ab")
            except OSError:
                err_f = None
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f or subprocess.DEVNULL, start_new_session=True,
            )
        return True
    except OSError:
        return False
def _spawn_detached_serve(idle_exit: float = 1800.0) -> bool:
    """拉起 detached 的向量库服务（数据唯一所有者）。

    与 worker 分开拉：serve 必须长驻单例，worker 天然短命——0.9.9 把两者塞进
    同一个进程，消费者退出就顺手把数据所有者带走了（见 PRD F2/F5）。

    失败只 stderr 留痕、返回 False：调用方（ensure_endpoint）会继续轮询，
    也许已有别的进程把它拉起来了。
    """
    from hymem.core import install_ops

    argv = install_ops.target_command() + ["serve", "--idle-exit", str(idle_exit)]
    try:
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW：不闪黑框
            try:
                logs = store.data_dir() / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                err_f: object = open(logs / "serve.err", "ab")
            except OSError:
                err_f = subprocess.DEVNULL
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f, creationflags=flags, close_fds=True,
            )
        else:
            err_f = None
            try:
                logs = store.data_dir() / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                err_f = open(logs / "serve.err", "ab")
            except OSError:
                err_f = None
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f or subprocess.DEVNULL, start_new_session=True,
            )
        return True
    except OSError:
        return False
