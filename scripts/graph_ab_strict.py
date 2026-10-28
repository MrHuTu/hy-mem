"""严格对照：同一沙箱同一数据，只切 reader（legacy vs hybrid+图重排）。
消除写入随机性混杂。"""
import json
import os
import sys
import time
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import hy_mem_cli as cli  # noqa: E402

QUERIES = [
    ("iisw_be 用什么框架和技术栈", ["Spring Boot", "yudao"]),
    ("生产环境和测试环境分别用什么数据库", ["Kingbase8", "H2"]),
    ("审批流为什么放弃 Activiti", ["Activiti", "兼容"]),
    ("FlowExecutor 是谁设计的", ["张伟"]),
    ("FSM 框架 v2.0 增加了什么", ["LogFlowListener"]),
    ("短信模板号是多少", ["SMS_512581036"]),
    ("流程定义迁移分几期怎么安排", ["两期", "报名"]),
    ("FSM 各版本分别增加了什么能力", ["v1.2", "v2.0"]),
    ("Excel 导入模板有多少列谁评审的", ["42", "陈静"]),
    ("Jenkins 流水线谁维护发布窗口什么时候", ["赵磊", "周四"]),
    ("密码字段怎么加密的", ["SM4"]),
    ("审批日志写到哪里", ["system_flow_log"]),
]


def replay(reader):
    sb = sorted(glob.glob(r"C:/Users/Administrator/AppData/Local/Temp/hymem-final-graph-*"))[-1]
    os.environ.update({
        "HY_MEM_CONFIG": os.path.join(sb, "config.json"),
        "MEMORY_DATA_DIR": sb, "HY_MEM_HOME": sb,
        "MEMORY_ENABLE_GRAPH": "1",
    })
    if reader == "hybrid":
        os.environ.update({"HY_MEMORY_READER": "hybrid", "MEMORY_GRAPH_RERANK_ENABLED": "1"})
    else:
        os.environ.update({"HY_MEMORY_READER": "legacy"})
        os.environ.pop("MEMORY_GRAPH_RERANK_ENABLED", None)
    cli.load_env()
    dep = json.load(open("D:/tools_open/hy-mem-0.9.8-win64/config.json", encoding="utf-8"))
    os.environ["MEMORY_LLM_BASE_URL"] = dep["embedding"]["baseUrl"]
    os.environ["MEMORY_LLM_MODEL"] = "qwen3.8-flash"
    os.environ["MEMORY_LLM_API_KEY"] = dep["embedding"]["apiKey"]

    client = cli.make_client(None)
    results = []
    for q, kws in QUERIES:
        t1 = time.time()
        r = client.search(q, user_ids=["fin-graph"], agent_ids=["fin"], limit=5)
        ranked = [it.get("content") or "" for it in cli._flatten_search(r)]
        rank = None
        for pos, doc in enumerate(ranked):
            if all(k.lower() in doc.lower() for k in kws):
                rank = pos + 1
                break
        results.append({"q": q, "rank": rank, "lat": round(time.time() - t1, 2), "hits": len(ranked)})
        print(f"  [{reader:7s}] Q={q[:16]:18s} hits={len(ranked)} rank={rank} ({results[-1]['lat']}s)", flush=True)
    client.close()
    return results


def score(rs):
    mrr = sum(1 / r["rank"] for r in rs if r["rank"]) / len(rs)
    top1 = sum(1 for r in rs if r["rank"] == 1) / len(rs)
    top3 = sum(1 for r in rs if r["rank"] and r["rank"] <= 3) / len(rs)
    lat = sum(r["lat"] for r in rs) / len(rs)
    return {"MRR": round(mrr, 3), "Top1": round(top1, 3), "Top3": round(top3, 3), "lat": round(lat, 2)}


if __name__ == "__main__":
    rs_legacy = replay("legacy")
    rs_hybrid = replay("hybrid")
    sa, sb = score(rs_legacy), score(rs_hybrid)
    print("\n" + "=" * 66)
    print("严格对照：同一数据，legacy vs hybrid+图重排")
    print(f"{'指标':<10}{'legacy':>12}{'hybrid+图':>12}")
    for k in ("MRR", "Top1", "Top3", "lat"):
        print(f"{k:<12}{sa[k]:>12}{sb[k]:>12}")
    print("\n逐查询（rank: legacy vs hybrid）:")
    for ra, rb in zip(rs_legacy, rs_hybrid):
        d = ""
        if ra["rank"] and rb["rank"]:
            d = "  G↑" if rb["rank"] < ra["rank"] else ("  G↓" if rb["rank"] > ra["rank"] else "")
        elif ra["rank"] and not rb["rank"]:
            d = "  G丢"
        elif not ra["rank"] and rb["rank"]:
            d = "  G救"
        print(f"  {ra['q'][:20]:24s} L={ra['rank'] or '-'} H={rb['rank'] or '-'}{d}")
    print("STRICT_DONE")
