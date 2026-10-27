# -*- coding: utf-8 -*-
"""探针：连发几条 add，dump SDK 完整返回（含 agent_* 诊断字段），并对照 chroma 落库状态。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "cli"))

sandbox = Path(tempfile.mkdtemp(prefix="hymem-probe-"))
cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
cfg["storage"]["dataDir"] = str(sandbox / "data")
(sandbox / "cfg.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
os.environ["HY_MEM_CONFIG"] = str(sandbox / "cfg.json")
os.environ["HY_MEM_HOME"] = str(REPO)
os.environ["MEMORY_DATA_DIR"] = str(sandbox / "data")
print("sandbox:", sandbox)

import hy_mem_cli as cli  # noqa: E402

cli.load_env()  # 重放 config.json 里的密钥与模型 env（main() 同款）
client = cli.make_client(None)
for i in range(3):
    text = f"探针-{i}-{int(time.time())}：验证 L2 事实抽取是否静默失败"
    t0 = time.time()
    try:
        r = client.add(text, user_id="huchao", agent_id="default_agent",
                       session_id="probe", source="probe")
        keys = {k: r.get(k) for k in (
            "success", "memory_id", "memory_ids", "ops_summary", "agent_status",
            "agent_error_code", "agent_error", "l1_rollback", "partial_agent_persist",
            "extract_tokens", "reconcile_tokens", "elapsed_ms",
        ) if k in r}
        print(f"\n[{i}] {time.time()-t0:.1f}s", json.dumps(keys, ensure_ascii=False, indent=1))
    except Exception as e:  # noqa: BLE001
        print(f"[{i}] EXC {type(e).__name__}: {e}")
client.close()

import sqlite3  # noqa: E402

c = sqlite3.connect(str(sandbox / "data" / "data" / "vector_db" / "chroma.sqlite3"))
try:
    rows = c.execute(
        "SELECT m.id, m.string_value FROM embedding_metadata m WHERE m.key='status'"
    ).fetchall()
    print("\nchroma status rows:", rows)
    docs = c.execute(
        "SELECT id, string_value FROM embedding_metadata WHERE key='chroma:document' LIMIT 10"
    ).fetchall()
    for d in docs:
        print("doc:", d[0], d[1][:60])
finally:
    c.close()
