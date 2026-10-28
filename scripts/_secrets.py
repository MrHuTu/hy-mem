"""测试/探针脚本取密钥的唯一入口——只读环境变量，一个字节都不落盘。

为什么单独一个模块
----------------
这个仓库要上 GitHub。测试脚本一旦自己写 `apiKey = "sk-..."`，泄漏就只是时间
问题（人眼在几千行里看不出一个 51 字符的 token）。所以约定：

  * 脚本里**只允许出现变量名**，不允许出现任何密钥字面量；
  * 密钥统一从进程环境变量取，由使用者在自己机器上配好；
  * 缺密钥时**明确报错并给出配置命令**，绝不静默跳过——静默跳过会让「全绿」
    变成假象（本次之所以要这个模块，就是吃过「测试过了但过的是假的」的亏）。

两个变量名沿用 SDK 自己的约定（`cli/config.py` 与 hy-memory 都认这两个），
不另造新概念：

    MEMORY_EMBEDDER_API_KEY   向量模型（embedding）密钥
    MEMORY_LLM_API_KEY        事实抽取 / 推理模型密钥

Windows 上一次性配置（用户级，永久生效，重开终端后可用）：

    setx MEMORY_EMBEDDER_API_KEY "sk-..."
    setx MEMORY_LLM_API_KEY "sk-..."

临时只对当前 shell 生效：

    export MEMORY_EMBEDDER_API_KEY='sk-...'

用法
----
    from _secrets import require, sandbox_config, REDACTED

    key = require("MEMORY_LLM_API_KEY")        # 缺了直接退出并说明怎么配
    cfg = sandbox_config(real_cfg)             # 清空所有 apiKey 后再写沙箱
"""

from __future__ import annotations

import copy
import os

# 变量名集中在这里，别的脚本不要再拼字符串（拼错的变量名 = 密钥读不到 = 假绿）
LLM_KEY_ENV = "MEMORY_LLM_API_KEY"
EMBED_KEY_ENV = "MEMORY_EMBEDDER_API_KEY"

# 沙箱配置里 apiKey 字段的占位物。留空串而不是 REDACTED：产品侧把「空」认作
# 「走环境变量」，把 REDACTED 认作「用户真填了这个值」，后者会拿去建连接。
REDACTED = ""

# 从聊天窗口/文档粘密钥最容易带进来的不可见字符（与 cli/config.py 同口径）
_INVISIBLE = {
    "\ufeff": "BOM", "\u200b": "零宽空格", "\u200c": "零宽连接符",
    "\u200d": "零宽连接符", "\u00a0": "不换行空格",
    "\r": "回车", "\n": "换行", "\t": "制表符", " ": "空格",
}


def clean(raw: str) -> str:
    """去掉不可见字符。粘进来的密钥十次有八次带着尾随空格。"""
    return "".join(ch for ch in (raw or "") if ch not in _INVISIBLE)


def get(env_name: str) -> str:
    """读环境变量，没有就返回空串。"""
    return clean(os.getenv(env_name, ""))


def require(env_name: str) -> str:
    """必须有；否则带着「怎么配」的说明退出，而不是让测试跑出误导性结论。"""
    value = get(env_name)
    if value:
        return value
    raise SystemExit(
        f"缺少环境变量 {env_name}：这类测试会真的调用模型，没有密钥跑不出结论。\n"
        f"  Windows（永久生效，重开终端）：setx {env_name} \"sk-...\"\n"
        f"  只对当前 shell 生效：           export {env_name}='sk-...'\n"
        f"密钥只放环境变量，不要写进 config.json，更不要写进脚本。"
    )


def have(env_name: str) -> bool:
    return bool(get(env_name))


def have_all() -> bool:
    """两条链路是否都具备真实密钥——探针用它在缺件时给出「跳过原因」。"""
    return have(EMBED_KEY_ENV) and have(LLM_KEY_ENV)


def source_of(env_name: str) -> str:
    """密钥来源的一句话描述（只暴露变量名，不暴露值）。"""
    return f"环境变量 {env_name}" if have(env_name) else f"{env_name}（未设置）"


def sandbox_config(base: dict) -> dict:
    """复制一份配置并**抹掉所有密钥**，专供写临时沙箱 config.json。

    测试要的是「模型、baseUrl、维度」这些结构，密钥由环境变量提供即可。把密钥
    从沙箱文件里拿掉有两个好处：临时目录被误打包/误贴出来不含密钥；也顺手验证了
    「配置里没密钥、只靠环境变量」这条路真的能跑通。
    """
    cfg = copy.deepcopy(base)
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    if "apiKey" in emb:
        emb["apiKey"] = REDACTED
    if "apiKey" in llm:
        llm["apiKey"] = REDACTED
    return cfg


def describe(env_names: list[str] | None = None) -> str:
    """给人看的一行状态：哪些变量有值（只显示有无，不显示值）。"""
    names = env_names or [EMBED_KEY_ENV, LLM_KEY_ENV]
    return " · ".join(f"{n}={'已设置' if have(n) else '未设置'}" for n in names)


if __name__ == "__main__":
    # 直接运行当成自检：print 状态、验证沙箱配置确实不含密钥
    print("密钥环境变量：", describe())
    demo = {"embedding": {"apiKey": "***"}, "llm": {"apiKey": "***"}, "mode": "pro"}
    print("沙箱配置抹除效果：", sandbox_config(demo))
