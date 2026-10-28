"""对 exe 的 chroma server 做协议级探针：验证垫片是否真的在包里。

1) 起 exe 的 `serve`（数据所有者；0.9.10 起 worker 不再承担这个角色）
2) 用 dev venv 的 chromadb HttpClient 直连
3) get(include=['metadatas','embeddings']) —— 垫片不在就会 500
4) 顺带看 metadatas 里 supersedes/superseded_by 是否原样返回
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# exe 位置：优先 HYMEM_EXE，其次仓库内产物 dist/hy-mem/hy-mem.exe。
# 不写死本机安装路径——仓库要公开，写死等于把机器布局一起发出去。
EXE = Path(os.getenv("HYMEM_EXE") or (Path(__file__).resolve().parent.parent / "dist" / "hy-mem" / "hy-mem.exe"))
if not EXE.exists():
    sys.exit(f"找不到 hy-mem 可执行文件：{EXE}\n"
             f"先跑 python scripts/build_exe.py，或用 HYMEM_EXE=<路径> 指定。")
INSTALL = EXE.parent
PORT_FILE = INSTALL / "data" / "server.port"
NID = "b6644b83-9f3d-4038-96e2-79195882ed35"


def main() -> int:
    PORT_FILE.unlink(missing_ok=True)
    env = dict(os.environ)
    env.pop("MEMORY_VECTOR_HOST", None)
    env.pop("MEMORY_VECTOR_PORT", None)
    w = subprocess.Popen([str(EXE), "serve", "--idle-exit", "60"],
                         cwd=str(INSTALL), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = None
    for _ in range(200):
        try:
            port = int(json.loads(PORT_FILE.read_text(encoding="utf-8"))["port"])
            break
        except Exception:
            pass
        if w.poll() is not None:
            print("serve died", w.returncode)
            return 2
        time.sleep(0.2)
    if not port:
        print("no port")
        w.kill()
        return 3
    print("server port:", port)

    import chromadb
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    cli = chromadb.HttpClient(host="127.0.0.1", port=port)
    col = cli.get_collection("agent_memories_1024")
    print("count:", col.count())

    for label, include in (("meta only", ["metadatas"]),
                           ("meta+emb", ["metadatas", "embeddings"])):
        try:
            r = col.get(ids=[NID], include=include)
            md = (r.get("metadatas") or [{}])[0]
            emb = r.get("embeddings")
            print(f"[{label}] OK superseded_by={md.get('superseded_by')!r} "
                  f"emb={'None' if emb is None else len(emb)}")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] FAIL {type(e).__name__}: {str(e)[:200]}")

    w.terminate()
    try:
        w.wait(timeout=10)
    except subprocess.TimeoutExpired:
        w.kill()
    PORT_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
