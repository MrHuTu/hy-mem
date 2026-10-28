"""图信号 A/B 实验（第 3 轮·修正版）

修正点（对第 2 轮的根因修复）：
1. 上轮 12 条孤立短事实：每条只抽 1 个 L2 fact → 无批内 co_occurs 边；
   且事实间无关系 → LLM 无 related 可标。本轮用长文本一次写入多条事实
   （批内 facts 两两相连=co_occurs，纯本地零依赖），且事实链有明确
   causes/same_series/contrasts 关系。
2. 查询集改为关联型（因果/演进/取舍），纯向量先天弱、图信号应有增益空间。
3. B 组写入后轮询 Kuzu 边数稳定（不只节点数），确认 related/co_occurs 落地。

两组：A=pro 纯向量(legacy reader)；B=pro+图+hybrid reader+图重排。
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import hy_mem_cli as cli  # noqa: E402

# ---- 数据集：关系密集（因果链/版本序列/取舍对比/人员分工/干扰项）----
FACTS = [
    # 长文本1：批内多事实（Activiti 兼容性原因 + 张伟设计 + FlowExecutor + flow_log）→ co_occurs
    "iisw_be 项目审批流重构决定采用自研 FSM 框架替代 Activiti，原因是 Activiti 在 Kingbase8 数据库上存在兼容性问题。"
    "FSM 框架由张伟主导设计，核心类是 FlowExecutor，入口方法为 fireAndPersist。审批流转日志统一写入 system_flow_log 表。",
    # 长文本2：因果（因=切 FSM，果=迁移分期）→ 期望 related: causes
    "由于审批流切换到自研 FSM 框架，原先基于 Activiti 的 23 个流程定义需要全部迁移。迁移分两期：第一期覆盖招商活动报名域，第二期覆盖企业服务域。",
    # 长文本3：同系列版本演进 → 期望 related: same_series
    "FSM 框架版本演进记录：v1.0 实现基本状态流转；v1.2 增加并行网关与会签支持；v2.0 引入 LogFlowListener 实现自动留痕写入。",
    # 长文本4：对比取舍 → 期望 related: contrasts
    "与芋道原生的 BPM 能力相比，自研 FSM 牺牲了可视化流程设计器，但换来了对 Kingbase8 的完全兼容与更快的审批吞吐，审批延迟从 800ms 降到 200ms。",
    # 干扰项：无关主题（数据库团队/环境），检验图信号是否误拉无关内容
    "李娜的团队负责数据库设计：主库使用 Kingbase8，测试环境用 H2 的 MySQL 兼容模式跑单元测试，密码字段采用 SM4 CBC 加密。",
]

# 关联型查询（纯向量先天弱：问的是关系/演进/取舍，不直接复述原文）
QUERIES = [
    ("审批流为什么放弃 Activiti 改用自研", 0, ["Activiti", "兼容"]),
    ("Activiti 流程定义迁移分几期怎么安排", 1, ["迁移", "期"]),
    ("FSM 框架各版本分别增加了什么能力", 2, ["v1.2", "v2.0"]),
    ("自研 FSM 与芋道 BPM 的取舍是什么", 3, ["设计器", "吞吐"]),
    ("FlowExecutor 是谁主导设计的", 0, ["张伟"]),
    ("审批日志写到哪张表", 0, ["system_flow_log"]),
    ("审批延迟优化效果如何", 3, ["800", "200"]),
    ("SM4 加密用在什么场景", 4, ["SM4"]),
]


def wait_graph_ready(sandbox: str, expect_nodes: int, timeout: float = 420) -> dict:
    """轮询 Kuzu：节点数≥expect 且 连续两轮边数不变 → 视为投影追平。"""
    import kuzu
    db_path = os.path.join(sandbox, "data", "data", "kuzu_db")
    deadline = time.time() + timeout
    last_edges = -1
    stable = 0
    while time.time() < deadline:
        try:
            db = kuzu.Database(db_path)
            conn = kuzu.Connection(db)
            r = conn.execute("MATCH (m:Memory) RETURN count(m) AS c").get_next()[0]
            e = conn.execute("MATCH ()-[r]->() RETURN count(r) AS c").get_next()[0]
            if r >= expect_nodes:
                if e == last_edges and e > 0:
                    stable += 1
                    if stable >= 2:
                        typed = conn.execute(
                            "MATCH ()-[r:RELATED_TO]->() RETURN count(r) AS c"
                        ).get_next()[0]
                        co = conn.execute(
                            "MATCH ()-[r:CO_OCCURS]->() RETURN count(r) AS c"
                        ).get_next()[0]
                        return {"nodes": r, "edges": e, "typed": typed, "co_occurs": co}
                else:
                    stable = 0
                last_edges = e
        except Exception:
            pass
        time.sleep(6)
    return {"nodes": -1, "edges": -1, "typed": -1, "co_occurs": -1}


def run(tag: str, graph: bool):
    sandbox = tempfile.mkdtemp(prefix=f"hymem-ab3-{tag}-")
    cfg = json.load(open("config.json", encoding="utf-8"))
    cfg["dataDir"] = sandbox
    json.dump(cfg, open(os.path.join(sandbox, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    env = {
        **os.environ,
        "HY_MEM_CONFIG": os.path.join(sandbox, "config.json"),
        "MEMORY_DATA_DIR": sandbox,
        "HY_MEM_HOME": sandbox,
        "MEMORY_USER": f"ab3-{tag}",
    }
    if graph:
        env.update({
            "MEMORY_ENABLE_GRAPH": "1",
            "HY_MEMORY_READER": "hybrid",
            "MEMORY_GRAPH_RERANK_ENABLED": "1",
        })
    for k, v in env.items():
        os.environ[k] = v
    cli.load_env()

    # LLM 固定用阿里 qwen3.8-flash（稳定不抖动；key 取自部署 config.json 的阿里端点）
    import json as _json
    dep = _json.load(open("D:/tools_open/hy-mem-0.9.8-win64/config.json", encoding="utf-8"))
    os.environ["MEMORY_LLM_BASE_URL"] = dep["embedding"]["baseUrl"]
    os.environ["MEMORY_LLM_MODEL"] = "qwen3.8-flash"
    os.environ["MEMORY_LLM_API_KEY"] = dep["embedding"]["apiKey"]
    os.environ["MEMORY_LLM_MAX_TOKENS"] = "4096"
    os.environ["MEMORY_AGENT_MAX_TOKENS"] = "4000"

    client = cli.make_client(None)
    t0 = time.time()
    for i, text in enumerate(FACTS):
        for attempt in range(3):  # ysjx 偶发 EMPTY_RESPONSE，退避重试
            res = client.add(text, user_id=f"ab3-{tag}", agent_id="ab", session_id="ab3")
            code = res.get("agent_error_code")
            if not code:
                break
            print(f"[{tag}] add#{i} {code}，重试 {attempt+1}", flush=True)
            time.sleep(5 * (attempt + 1))
        print(f"[{tag}] add#{i} ids={len(res.get('memory_ids') or [])} "
              f"err={res.get('agent_error_code')} ({time.time()-t0:.0f}s)", flush=True)
    client.close()
    print(f"[{tag}] 写入完成 {time.time()-t0:.0f}s", flush=True)

    ginfo = None
    if graph:
        ginfo = wait_graph_ready(sandbox, expect_nodes=6)
        print(f"[{tag}] 图状态: {ginfo}", flush=True)

    # 检索（新 client，模拟冷启动检索）
    client = cli.make_client(None)
    results = []
    for query, want_idx, kws in QUERIES:
        t1 = time.time()
        r = client.search(query, user_ids=[f"ab3-{tag}"], agent_ids=["ab"], limit=5)
        ranked = [it.get("content") or "" for it in cli._flatten_search(r)]
        # 判分：期望 fact 的关键词组全部出现在某条结果里 → 找到其排名
        rank = None
        for pos, doc in enumerate(ranked):
            if all(k.lower() in doc.lower() for k in kws):
                rank = pos + 1
                break
        results.append({"q": query, "want": want_idx, "rank": rank,
                        "latency": round(time.time() - t1, 2)})
        print(f"[{tag}] Q={query[:18]}… rank={rank} lat={results[-1]['latency']}s", flush=True)
    client.close()
    return sandbox, ginfo, results


def score(results):
    mrr = sum(1 / (r["rank"]) if r["rank"] else 0 for r in results) / len(results)
    top1 = sum(1 for r in results if r["rank"] == 1) / len(results)
    top3 = sum(1 for r in results if r["rank"] and r["rank"] <= 3) / len(results)
    lat = sum(r["latency"] for r in results) / len(results)
    return {"MRR": round(mrr, 3), "Top1": round(top1, 3), "Top3": round(top3, 3),
            "avg_lat": round(lat, 2)}


if __name__ == "__main__":
    keep = []
    for tag, g in (("vec", False), ("graph", True)):
        sb, gi, rs = run(tag, g)
        keep.append((tag, sb, gi, rs))
        print(f"\n===== {tag} ===== graph={gi} score={score(rs)}\n", flush=True)

    print("\n" + "=" * 62)
    print("第 3 轮 A/B 对比（关系密集数据 + 关联型查询）")
    print(f"{'指标':<8}{'A 纯向量':>12}{'B 图重排':>12}")
    sa, sb_ = score(keep[0][3]), score(keep[1][3])
    for k in ("MRR", "Top1", "Top3", "avg_lat"):
        print(f"{k:<10}{sa[k]:>12}{sb_[k]:>12}")
    print(f"图状态(B): {keep[1][2]}")
    print(f"沙箱: A={keep[0][1]}")
    print(f"      B={keep[1][1]}")
