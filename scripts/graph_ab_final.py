"""图信号 A/B 正式对照（大数据量版）

数据设计（10 次 add，约 30+ 事实，全部围绕同一项目世界）：
- 同项目/同人物/同技术栈高频共现 → reconcile 候选相似度 > 0.55 → semantic_neighbor 边
- 长多事实文本 → 批内多 fact → co_occurs 边
- 版本演进/因果链文本 → 可能触发 LLM related 标注
- 查询集 12 个：6 直查 + 6 关联型（多跳/演进/取舍）

边统计从真实表名取：RELATED_TO / TAGGED_WITH / TEMPORAL_NEXT / MENTIONS / HAS_MEMORY。
LLM 固定 qwen3.8-flash（阿里官方端点，稳定）。
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import hy_mem_cli as cli  # noqa: E402

FACTS = [
    "iisw_be 是云上江西招商系统的后端项目，基于芋道 yudao 框架，技术栈 Spring Boot 2.7.18 加 JDK 8，采用 Controller/Service/DAL 三层架构。项目经理是周敏，架构师是刘洋。",
    "iisw_be 的 manage 域采用非多租户设计，错误码区间 200_100_XX，BaseDO 不含 tenant_id，权限校验下沉到 Service 层做 creator 校验。",
    "iisw_be 生产环境数据库是 Kingbase8，测试环境用 H2 的 MySQL 兼容模式跑单元测试，数据库负责人是李娜，密码字段采用 SM4 CBC 加密。",
    "iisw_be 审批流重构：放弃 Activiti 改用自研 FSM 框架，原因是 Activiti 在 Kingbase8 上有兼容性问题。FSM 框架由张伟主导设计，核心类 FlowExecutor，入口方法 fireAndPersist，审批日志写入 system_flow_log 表。",
    "FSM 框架版本演进：v1.0 实现基本状态流转；v1.2 增加并行网关与会签支持；v2.0 引入 LogFlowListener 实现自动留痕。每版的回归测试由王芳的测试组负责。",
    "由于审批流切换自研 FSM，Activiti 的 23 个流程定义需要分两期迁移：第一期覆盖招商活动报名域，第二期覆盖企业服务域。迁移负责人是赵磊。",
    "iisw_be 招商活动报名域支持 Excel 批量导入，模板 42 列，含导出-导入回环校验。该功能由陈静负责需求评审，张伟负责前端开发。",
    "iisw_be 网站报名流程：初审通过后短信通知用户补充资料，短信模板号 SMS_512581036，走的是阿里云短信通道。",
    "iisw_be 部署拓扑：生产环境 4 台应用节点加 SLB，Jenkins 流水线由赵磊维护，发布窗口每周四晚上十点。测试环境单节点，端口 48080。",
    "iisw_be 的 API 纪律：红测试先行，实现转绿后才能 commit，每个切片独立 commit 不混提，闸门验收通过才进下一步，代码与文档分两次提交。",
]

# (查询, 期望命中的关键词组)
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


def graph_edge_stats(sandbox):
    import kuzu
    db = kuzu.Database(os.path.join(sandbox, "data", "data", "kuzu_db"))
    conn = kuzu.Connection(db)
    out = {}
    for rel in ("RELATED_TO", "TAGGED_WITH", "TEMPORAL_NEXT", "MENTIONS", "HAS_MEMORY"):
        try:
            out[rel] = conn.execute(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c").get_next()[0]
        except Exception:
            out[rel] = -1
    try:
        out["Memory"] = conn.execute("MATCH (m:Memory) RETURN count(m) AS c").get_next()[0]
    except Exception:
        out["Memory"] = -1
    return out


def run(tag, graph):
    print(f"\n===== 组 {tag}（graph={graph}）=====", flush=True)
    sandbox = tempfile.mkdtemp(prefix=f"hymem-final-{tag}-")
    cfg = json.load(open("config.json", encoding="utf-8"))
    cfg["dataDir"] = sandbox
    json.dump(cfg, open(os.path.join(sandbox, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    os.environ.update({
        "HY_MEM_CONFIG": os.path.join(sandbox, "config.json"),
        "MEMORY_DATA_DIR": sandbox, "HY_MEM_HOME": sandbox,
    })
    if graph:
        os.environ.update({
            "MEMORY_ENABLE_GRAPH": "1", "HY_MEMORY_READER": "hybrid",
            "MEMORY_GRAPH_RERANK_ENABLED": "1",
        })
    else:
        for k in ("MEMORY_ENABLE_GRAPH", "HY_MEMORY_READER", "MEMORY_GRAPH_RERANK_ENABLED"):
            os.environ.pop(k, None)
    cli.load_env()
    dep = json.load(open("D:/tools_open/hy-mem-0.9.8-win64/config.json", encoding="utf-8"))
    os.environ["MEMORY_LLM_BASE_URL"] = dep["embedding"]["baseUrl"]
    os.environ["MEMORY_LLM_MODEL"] = "qwen3.8-flash"
    os.environ["MEMORY_LLM_API_KEY"] = dep["embedding"]["apiKey"]
    os.environ["MEMORY_LLM_MAX_TOKENS"] = "4096"
    os.environ["MEMORY_AGENT_MAX_TOKENS"] = "4000"

    client = cli.make_client(None)
    t0 = time.time()
    fail = 0
    for i, text in enumerate(FACTS):
        res = client.add(text, user_id=f"fin-{tag}", agent_id="fin", session_id="fin")
        code = res.get("agent_error_code")
        if code:
            fail += 1
            print(f"  add#{i} err={code}，重试", flush=True)
            time.sleep(5)
            res = client.add(text, user_id=f"fin-{tag}", agent_id="fin", session_id="fin")
            code = res.get("agent_error_code")
        print(f"  add#{i} ids={len(res.get('memory_ids') or [])} err={code} ({time.time()-t0:.0f}s)", flush=True)
    client.close()
    print(f"  写入完成 {time.time()-t0:.0f}s，失败 {fail}", flush=True)

    gs = None
    if graph:
        # 轮询投影：边总数连续 3 轮稳定且 Memory 节点不再增长
        deadline = time.time() + 600
        last_total = -1
        stable = 0
        while time.time() < deadline:
            try:
                gs = graph_edge_stats(sandbox)
                total = sum(max(0, v) for k, v in gs.items() if k != "Memory")
                if total == last_total and total > 0:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                last_total = total
            except Exception:
                pass
            time.sleep(8)
        print(f"  图状态: {gs}", flush=True)

    client = cli.make_client(None)
    results = []
    for q, kws in QUERIES:
        t1 = time.time()
        r = client.search(q, user_ids=[f"fin-{tag}"], agent_ids=["fin"], limit=5)
        ranked = [it.get("content") or "" for it in cli._flatten_search(r)]
        rank = None
        for pos, doc in enumerate(ranked):
            if all(k.lower() in doc.lower() for k in kws):
                rank = pos + 1
                break
        results.append({"q": q, "rank": rank, "lat": round(time.time() - t1, 2)})
        print(f"  Q={q[:16]:18s} hits={len(ranked)} rank={rank} ({results[-1]['lat']}s)", flush=True)
    client.close()
    return sandbox, gs, results


def score(rs):
    mrr = sum(1 / r["rank"] for r in rs if r["rank"]) / len(rs)
    top1 = sum(1 for r in rs if r["rank"] == 1) / len(rs)
    top3 = sum(1 for r in rs if r["rank"] and r["rank"] <= 3) / len(rs)
    lat = sum(r["lat"] for r in rs) / len(rs)
    return {"MRR": round(mrr, 3), "Top1": round(top1, 3), "Top3": round(top3, 3), "lat": round(lat, 2)}


if __name__ == "__main__":
    sb_a, _, rs_a = run("vec", False)
    sb_b, gs, rs_b = run("graph", True)

    sa, sb = score(rs_a), score(rs_b)
    print("\n" + "=" * 66)
    print("正式 A/B 对照（10 add × ~30 事实 × 12 查询，qwen3.8-flash）")
    print(f"{'指标':<10}{'A 纯向量':>12}{'B 图重排':>12}")
    for k in ("MRR", "Top1", "Top3", "lat"):
        print(f"{k:<12}{sa[k]:>12}{sb[k]:>12}")
    print(f"B 图边统计: {gs}")
    print(f"\n逐查询对比（rank: A vs B）:")
    for ra, rb in zip(rs_a, rs_b):
        d = ""
        if ra["rank"] and rb["rank"]:
            d = "  B↑" if rb["rank"] < ra["rank"] else ("  B↓" if rb["rank"] > ra["rank"] else "")
        elif ra["rank"] and not rb["rank"]:
            d = "  B丢"
        elif not ra["rank"] and rb["rank"]:
            d = "  B救"
        print(f"  {ra['q'][:20]:24s} A={ra['rank'] or '-'} B={rb['rank'] or '-'}{d}")
    print(f"\n沙箱 A={sb_a}\n     B={sb_b}")
    print("FINAL_DONE")
