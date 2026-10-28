"""A/B 对照逻辑冒烟版：小数据量跑通全链路，每环节显式断言。

环节：
S1 add 落库（chroma 出现 l2_fact 行）
S2 B组图投影（Kuzu 边数>0，区分 co_occurs/typed）
S3 检索返回非空（用 cli._flatten_search 解析）
S4 判分逻辑（关键词组命中排名）
S5 A/B 对比表输出

用法：.venv/Scripts/python.exe scripts/graph_ab_smoke.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import hy_mem_cli as cli  # noqa: E402

FACTS = [
    # 一条长文本含多个事实（触发批内 co_occurs）+ 明确因果
    "iisw_be 项目审批流重构采用自研 FSM 框架替代 Activiti，原因是 Activiti 在 Kingbase8 上有兼容性问题。核心类 FlowExecutor 由张伟设计，入口方法 fireAndPersist，审批日志写入 system_flow_log 表。",
    # 关联事实（与上面构成同主题系列）
    "由于 FSM 框架上线，原先 Activiti 的 23 个流程定义需要分两期迁移：第一期覆盖招商活动报名域，第二期覆盖企业服务域。",
]

QUERIES = [
    ("审批流为什么放弃 Activiti", ["Activiti", "兼容"]),
    ("FlowExecutor 谁设计的", ["张伟"]),
    ("流程定义迁移分几期", ["迁移", "期"]),
]


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    return ok


def chroma_l2_count(sandbox):
    import sqlite3
    p = os.path.join(sandbox, "data", "data", "vector_db", "chroma.sqlite3")
    c = sqlite3.connect(p)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM embedding_metadata WHERE key='layer' AND string_value='l2_fact'"
        ).fetchone()[0]
    finally:
        c.close()


def graph_stats(sandbox):
    import kuzu
    db = kuzu.Database(os.path.join(sandbox, "data", "data", "kuzu_db"))
    conn = kuzu.Connection(db)
    out = {}
    for label in ("CO_OCCURS", "RELATED_TO", "TAGGED_WITH"):
        try:
            out[label] = conn.execute(f"MATCH ()-[r:{label}]->() RETURN count(r) AS c").get_next()[0]
        except Exception:
            out[label] = -1
    return out


def run(tag, graph):
    print(f"\n===== 组 {tag}（graph={graph}）=====", flush=True)
    sandbox = tempfile.mkdtemp(prefix=f"hymem-smoke-{tag}-")
    cfg = json.load(open("config.json", encoding="utf-8"))
    cfg["dataDir"] = sandbox
    json.dump(cfg, open(os.path.join(sandbox, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    os.environ.update({
        "HY_MEM_CONFIG": os.path.join(sandbox, "config.json"),
        "MEMORY_DATA_DIR": sandbox, "HY_MEM_HOME": sandbox,
    })
    cli.load_env()
    dep = json.load(open("D:/tools_open/hy-mem-0.9.8-win64/config.json", encoding="utf-8"))
    os.environ["MEMORY_LLM_BASE_URL"] = dep["embedding"]["baseUrl"]
    os.environ["MEMORY_LLM_MODEL"] = "qwen3.8-flash"
    os.environ["MEMORY_LLM_API_KEY"] = dep["embedding"]["apiKey"]
    os.environ["MEMORY_LLM_MAX_TOKENS"] = "4096"
    os.environ["MEMORY_AGENT_MAX_TOKENS"] = "4000"
    if graph:
        os.environ.update({
            "MEMORY_ENABLE_GRAPH": "1", "HY_MEMORY_READER": "hybrid",
            "MEMORY_GRAPH_RERANK_ENABLED": "1",
        })

    # S1 写入
    client = cli.make_client(None)
    t0 = time.time()
    ok_add = True
    for i, text in enumerate(FACTS):
        res = client.add(text, user_id=f"sm-{tag}", agent_id="sm", session_id="sm")
        code = res.get("agent_error_code")
        if code:
            ok_add = False
        print(f"  add#{i} ids={len(res.get('memory_ids') or [])} err={code} ({time.time()-t0:.0f}s)", flush=True)
    client.close()
    l2 = chroma_l2_count(sandbox)
    check(f"S1 落库 l2_fact={l2}", ok_add and l2 >= 1)

    # S2 图投影等待（仅 B 组）
    gs = None
    if graph:
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                gs = graph_stats(sandbox)
                total = max(0, gs["CO_OCCURS"]) + max(0, gs["RELATED_TO"]) + max(0, gs["TAGGED_WITH"])
                if total > 0:
                    break
            except Exception:
                gs = None
            time.sleep(5)
        print(f"  图状态: {gs}", flush=True)
        check("S2 图投影有边", gs is not None and (gs["CO_OCCURS"] > 0 or gs["RELATED_TO"] > 0 or gs["TAGGED_WITH"] > 0))

    # S3/S4 检索与判分
    client = cli.make_client(None)
    ranks = []
    for q, kws in QUERIES:
        t1 = time.time()
        r = client.search(q, user_ids=[f"sm-{tag}"], agent_ids=["sm"], limit=5)
        ranked = [it.get("content") or "" for it in cli._flatten_search(r)]
        rank = None
        for pos, doc in enumerate(ranked):
            if all(k.lower() in doc.lower() for k in kws):
                rank = pos + 1
                break
        ranks.append(rank)
        print(f"  Q={q[:16]}… hits={len(ranked)} rank={rank} ({time.time()-t1:.2f}s)", flush=True)
    client.close()
    check("S3 检索返回非空", any(r is not None for r in ranks))
    return sandbox, gs, ranks


if __name__ == "__main__":
    ok_all = True
    sb_a, _, ranks_a = run("vec", False)
    sb_b, gs, ranks_b = run("graph", True)

    print("\n===== S5 A/B 对比 =====", flush=True)
    print(f"{'查询':<22}{'A排名':>6}{'B排名':>6}")
    for (q, _), ra, rb in zip(QUERIES, ranks_a, ranks_b):
        print(f"{q:<22}{str(ra or '-'):>6}{str(rb or '-'):>6}")
    for name, rs in (("A", ranks_a), ("B", ranks_b)):
        mrr = sum(1 / r for r in rs if r) / len(rs)
        top1 = sum(1 for r in rs if r == 1) / len(rs)
        print(f"{name}: MRR={mrr:.3f} Top1={top1:.3f}")
    print(f"\n沙箱 A={sb_a}\n沙箱 B={sb_b}")
    print("SMOKE_DONE")
