import json
import math
import urllib.request

KEY = "sk-ws-H.YHMRXMM.Q3Xv.MEUCIQCpdXnYDjeFddFoQldPXS2LvunCurwj5_odLbse3-UhjgIgVDtUUtGg7pcgrtSgNqV7jkYBKVThXymf1zbxX4JYX_c"
BASE = "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1"

QUERY = "活动报名的审批流怎么落库"
DOCS = [
    "招商活动报名模块的审批流用自研 FSM，FlowExecutor.fireAndPersist 落库并写 system_flow_log",
    "iisw_be2 项目用 test 分支，不开新特性分支；提交后不 push",
    "今天中午吃了牛肉面",
]


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return s / (na * nb)


def remote(model):
    body = json.dumps({"model": model, "input": DOCS + [QUERY]}).encode()
    req = urllib.request.Request(
        BASE + "/embeddings",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        d = json.loads(resp.read().decode())
    return [x["embedding"] for x in d["data"]]


rows = []
for m in ["qwen3.7-text-embedding-flash", "qwen3.7-text-embedding", "text-embedding-v4"]:
    v = remote(m)
    rows.append((m, len(v[0]), [cos(v[-1], x) for x in v[:-1]]))

from fastembed import TextEmbedding  # noqa: E402

lm = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5", cache_dir=".models")
lv = [[float(y) for y in x] for x in lm.embed(DOCS + [QUERY])]
rows.append(("local bge-small-zh", len(lv[0]), [cos(lv[-1], x) for x in lv[:-1]]))

hdr = f"{'model':<30}{'dims':>6}{'hit':>8}{'near':>8}{'noise':>8}{'ratio':>8}"
print(hdr)
print("-" * len(hdr))
for name, dim, p in rows:
    print(f"{name:<30}{dim:>6}{p[0]:>8.3f}{p[1]:>8.3f}{p[2]:>8.3f}{p[0]/max(p[1],1e-6):>7.2f}x")
