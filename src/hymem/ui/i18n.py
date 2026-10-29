"""界面文案的中英切换 —— 以中文原文当 key.

为什么用中文原文做键、而不是 msg_id：
  1. 现有代码里全是中文字面量，包一层 `tr()` 就完成改造，不必回头给每句话
     起 id，也就不会出现「id 改了译文没跟上」这类漂移；
  2. **缺译时原样返回中文**。界面退化成中文，而不是露出一串 msg_id 或抛
     KeyError —— 少翻译一句话不该让界面崩掉。

语言来源优先级：环境变量 HY_MEM_LANG > config.json 的 lang > 默认 zh。
优先级把环境变量放最前，和身份（HY_MEM_USER_ID）一致：临时换语言不用改配置，
`HY_MEM_LANG=en hy-mem` 一条命令就够。

刻意不做的事：不读 ui.language（那是历史字段，从未生效），也不把 HY_MEM_LANG
写进 config.to_env()——那份映射表是「配置覆盖环境」，而语言的优先级正好相反。
"""
from __future__ import annotations

import os

LANGS = ("zh", "en")
DEFAULT = "zh"

# 配置文件的 mtime 缓存：菜单一屏要翻几十句文案，每句都去读一次 config.json
# 太浪费；文件被改写（config set / 设置页切换）时 mtime 会变，自动失效。
_stamp: tuple[str, int, int] | None = None
_cached: str = DEFAULT


def _config_lang() -> str:
    global _stamp, _cached
    try:
        from hymem.core import config as cfgmod

        p = cfgmod.config_path()
        try:
            st = p.stat()
            stamp: tuple[str, int, int] | None = (str(p), st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp is not None and stamp == _stamp:
            return _cached
        v = str((cfgmod.load_raw() or {}).get("lang") or "").strip().lower()
        _cached = v if v in LANGS else DEFAULT
        _stamp = stamp
        return _cached
    except Exception:  # noqa: BLE001
        # 配置层坏了不该让界面起不来：退回中文，doctor 会报真正的原因
        return DEFAULT


def lang() -> str:
    """当前生效语言：env > config > zh。非法值按「没设」处理，继续往下找。"""
    v = os.getenv("HY_MEM_LANG", "").strip().lower()
    if v in LANGS:
        return v
    return _config_lang()


def set_lang(v: str) -> str:
    """写回 config.json 的 lang 字段并返回规范化后的值。"""
    global _stamp, _cached
    v = (v or "").strip().lower()
    if v not in LANGS:
        raise ValueError(f"lang 只能是 {'/'.join(LANGS)}，得到 {v!r}")
    from hymem.core import config as cfgmod

    cfg = cfgmod.load()
    cfg["lang"] = v
    cfgmod.save(cfg)
    _stamp, _cached = None, v
    return v


def tr(text: str, *args, **kw) -> str:
    """翻译一条文案。中文下原样返回（带占位符时按 args/kw 格式化）。

    参数用 `*args, **kw` 而不是只收 kw：绝大多数文案是
    `"共 {} 条"` 这种顺序占位，逐个起名字只会让调用处更啰嗦。
    """
    out = EN.get(text, text) if lang() == "en" else text
    if not args and not kw:
        return out
    try:
        return out.format(*args, **kw)
    except (KeyError, IndexError, ValueError):
        # 占位符数量对不上（多半是译文写错了）：退回未格式化的文本，
        # 宁可露出一处 {} ，也不能让一次渲染抛异常带走整个界面
        return out


def label(v: str | None = None) -> str:
    """语言自身的显示名（中文界面里也写中文，英文界面里写英文）。"""
    return "English" if (v or lang()) == "en" else "中文"


# 术语表（统一口径，避免同一句里混用两种说法）：
#   记忆=memory 检索=search 标签=tag 队列=queue 身份=identity
#   向量模型=embedding model 写入=write 拆条=split 落库=persist
#   阈值=threshold 设置=Settings 帮助=Help
EN: dict[str, str] = {
    # ---- 全局 / 导航 ----
    "记忆库命令行": "memory CLI",
    "选择操作": "Select action",
    "退出": "Quit",
    "Esc / q 也可以退出": "Esc / q also quits",
    "返回": "Back",
    "要做什么？": "What next?",
    "操作": "Action",
    "改哪一项？": "What to change?",
    "完成": "Done",
    "已取消": "Cancelled",
    "已中断": "Interrupted",
    "按任意键继续…": "Press any key to continue…",
    "该操作未执行（参数不合法）": "Not executed (invalid arguments)",
    "{} 出错：{}: {}": "{} failed: {}: {}",
    "失败": "Failed",
    "失败（exit {}）": "Failed (exit {})",
    "正在读取…": "Loading…",

    # ---- banner ----
    "模式": "Mode",
    "向量": "Embedding",
    "身份": "Identity",
    "库": "Store",
    "帮助": "Help",
    "{} 维": "{} dims",
    "（{}）": " ({})",
    "按 i 或运行 hy-mem help --json": "Press i, or run hy-mem help --json",
    "配置未完成：缺 {}": "Setup incomplete: missing {}",
    "→ 按 s 进设置，或运行 hy-mem setup": "→ press s for Settings, or run hy-mem setup",
    "纯向量，最快": "vector only, fastest",
    "向量+事实抽取": "vector + fact extraction",
    "再+Schema 归纳": "plus schema induction",

    # ---- 主菜单 ----
    "写记忆": "Write",
    "多行输入，可拆条 + 打标签": "Multi-line input, split and tag",
    "检索": "Search",
    "语义检索，可按标签过滤": "Semantic search, filter by tag",
    "最近记忆": "Recent",
    "最近 15 条，可复制 id 去删": "Last 15 memories; copy an id to delete",
    "标签": "Tags",
    "分类清单与解绑": "Inventory and unbind",
    "队列": "Queue",
    "异步写入的待处理与消费": "Pending async writes and consumption",
    "健康检查": "Health",
    "条数 / 体积 / 写入延迟预期": "Counts / size / expected write latency",
    "设置": "Settings",
    "模型 / 模式 / 存储目录": "Embedding model / mode / storage",
    "操作日志": "Operation log",
    "最近 50 条执行记录，可清空": "Last 50 runs, can be cleared",
    "环境自检": "Doctor",
    "doctor：端到端连通性": "End-to-end connectivity check",
    "当前配置": "Config",
    "只读查看生效配置": "Read-only view of effective config",
    "帮助 / 命令清单": "Help / commands",
    "命令清单 + 给智能体读的 JSON 规格入口": "Command list + JSON spec for agents",

    # ---- 写记忆 ----
    "写记忆（第 {} 条）": "Write ({})",
    "  一行一条最好（混在一起会稀释向量）。空行结束本条；Ctrl+C 取消。":
        "  One memory per line (mixing dilutes the vector). Blank line ends this one; Ctrl+C cancels.",
    "  接着写；不输入内容直接回车 = 回主界面。":
        "  Keep going; press Enter with nothing typed to return to the main menu.",
    "（没有输入内容，已回主界面）": "(Nothing entered — back to the main menu)",
    "检测到 {} 行，拆成 {} 条独立记忆？": "{} lines detected, split into {} memories?",
    "标签（逗号分隔，可留空）": "Tags (comma separated, optional)",
    "标签用于按类归档与过滤；pro 模式还会由 LLM 自动打 tag":
        "Tags group and filter memories; pro mode also auto-tags with the LLM",
    "入队异步写入？": "Queue this write?",
    "（推荐）": " (recommended)",
    "pro 模式同步写入每条 8~40s，建议入队":
        "pro mode writes each memory in 8-40s; queueing recommended",
    "写入中…": "Writing…",
    "已提交": "Submitted",
    "已入队，但还没进库 —— 此刻检索不到它是正常的。":
        "Queued but not persisted yet — not finding it right now is expected.",
    "现在就跑一次 worker 落库？（pro 模式每条约 10~50s）":
        "Run the worker now to persist? (pro mode: ~10-50s each)",
    "落库中…": "Persisting…",
    "已落库": "Persisted",

    # ---- 检索 ----
    "查询内容": "Query",
    "留空回车 = 回主界面；查完可直接接着查下一次":
        "Enter to leave blank = back to main menu; after a search just type the next one",
    "留空回车 = 回主界面": "Enter to leave blank = back to main menu",
    "限定标签（逗号分隔，可留空）": "Filter by tags (comma separated, optional)",
    "返回条数": "Number of results",
    "无命中": "No matches",
    "可试着放宽：降低阈值（设置 → 检索阈值）或换更接近原话的问法":
        "Try widening: lower the threshold (Settings → Threshold) or rephrase closer to the original wording",
    "#": "#",
    "分数": "Score",
    "内容": "Content",
    "阈值 {}，命中 {} 条；看全文：hy-mem get {}":
        "Threshold {}, {} matches; full text: hy-mem get {}",

    # ---- 最近记忆 ----
    "（空）": "(empty)",
    "共 {} 条；删除用 hy-mem delete <id>": "{} total; delete with hy-mem delete <id>",

    # ---- 标签 ----
    "（还没有标签）": "(no tags yet)",
    "合计": "Total",
    "手工": "Manual",
    "自动": "Auto",
    "从记忆同步自动标签": "Sync auto tags from memories",
    "把 SDK 侧 LLM 生成的 tag 拉进本地索引": "Pull LLM-generated tags from the SDK into the local index",
    "删除某个标签": "Delete a tag",
    "只解绑，不删记忆": "Unbind only, memories are kept",
    "要删除的标签名": "Tag to delete",
    "解绑所有 {} 标签？": "Unbind tag {} everywhere?",

    # ---- 队列 ----
    "异步队列": "Async queue",
    "待处理": "Pending",
    "处理中": "Running",
    "已完成": "Done",
    "死信": "Dead letter",
    "最老待处理": "Oldest pending",
    "处理队列直到清空": "Process queue until empty",
    "跑 worker，逐条落库（pro 每条 8~40s）": "Run the worker, persist one by one (pro: 8-40s each)",
    "只处理一条": "Process one job",
    "处理 1 条后返回": "Handle 1 job, then return",
    "重投死信任务": "Retry dead jobs",
    "把 dead 任务放回待处理": "Move dead jobs back to pending",
    "开始消费；Ctrl+C 可中断（已入队的任务不会丢）":
        "Consuming; Ctrl+C interrupts (queued jobs are safe)",
    "消费中…": "Consuming…",
    "处理一条…": "Processing one…",
    "把状态为 dead 的任务全部重投？": "Requeue all dead jobs?",

    # ---- 健康检查 ----
    "扫描库体积与条数…": "Scanning size and counts…",
    "  结论 {}   ": "  Verdict {}   ",
    "{} 条 / {} MB / 单条预期 {}s": "{} memories / {} MB / expected {}s per write",
    "项": "Item",
    "值": "Value",
    "说明": "Detail",
    "建议": "Advice",

    # ---- 存储目录 ----
    "数据存储目录": "Storage directory",
    "换目录 / 迁移数据": "Change / migrate directory",
    "复制现有数据到新位置并切换": "Copy existing data to a new location and switch",
    "恢复到默认目录": "Reset to default directory",
    "备份一份": "Make a backup",
    "复制成 data.bak-时间戳，改模型前用": "Copy to data.bak-<timestamp>; do this before changing models",
    "清空重建": "Wipe and rebuild",
    "先自动备份，再清空（重建索引/换维度后使用）":
        "Auto-backup, then wipe (after rebuilding the index or changing dims)",
    "清理队列库": "Vacuum queue DB",
    "删除已完成任务记录并 VACUUM，只影响队列不影响记忆":
        "Delete finished jobs and VACUUM; touches the queue only",
    "清理 jobs 表里已完成的记录并 VACUUM？（不动记忆）":
        "Delete finished jobs and VACUUM? (memories untouched)",
    "队列记录已清理": "Queue records cleared",
    "先备份再清空数据目录？清空后记忆需要重新写入":
        "Back up, then wipe the data directory? Memories must be written again",
    "新目录": "New directory",
    "留空取消；支持 %USERPROFILE% 与 ~": "Blank cancels; %USERPROFILE% and ~ supported",
    "与当前目录相同，无需切换": "Same as the current directory, nothing to switch",
    "把现有数据（{} / {} 文件）迁移过去？": "Migrate existing data ({} / {} files)?",
    "复制中…": "Copying…",
    "迁移失败，仍然切换？": "Migration failed; switch anyway?",
    "存储目录已切到 {}": "Storage directory switched to {}",
    "当前目录": "Current",
    "已占用": "Used",
    "{} / {} 个文件": "{} / {} files",
    "空目录": "Empty directory",
    "所在盘可用": "Free on disk",
    "默认目录": "Default",

    # ---- 身份 ----
    "身份 user_id": "Identity user_id",
    "当前身份": "Current identity",
    "生效来源": "Source",
    "配置文件": "Config file",
    "环境变量 HY_MEM_USER_ID": "env HY_MEM_USER_ID",
    "未设置 → 通用": "unset → general",
    "通用（default）": "General (default)",
    "通用（{}）": "General ({})",
    "其它名字": "Other names",
    "所有智能体共享同一份记忆 —— 不设 user_id 的默认行为":
        "Every agent shares one memory — the default when user_id is unset",
    "自成一套记忆，与别处互不可见，用于隔离":
        "Its own separate memory, invisible elsewhere — for isolation",
    "切换只改身份：模型配置、数据目录、已有记忆都不动；":
        "Switching changes identity only: models, data dir and memories stay put",
    "原身份的记忆不会删，只是现在看不到，切回去就能看到。":
        "The previous identity's memories are kept, just hidden until you switch back.",
    "改成另一个 user_id": "Switch to another user_id",
    "为这个智能体单独隔离一份记忆": "Give this agent an isolated memory",
    "回到通用（default）": "Back to general (default)",
    "与其它智能体共享": "Share with other agents",
    "查看见过哪些身份": "List identities seen",
    "只读": "Read-only",
    "当前": "Current",
    "（还没有记录到别的身份）": "(no other identities recorded)",
    "回到通用（{}）？": "Back to general ({})?",
    "已切到 {}": "Switched to {}",
    "新的 user_id": "New user_id",
    "英文/数字/-/_ 最稳；留空取消": "Letters/digits/-/_ work best; blank cancels",
    "{} 的记忆没有删，切回去仍能看到": "{} memories were not deleted; switch back to see them",

    # ---- 设置 ----
    "向量模型": "Embedding model",
    "运行模式": "Mode",
    "LLM 模型": "LLM model",
    "存储目录": "Storage",
    "检索阈值": "Threshold",
    "界面语言": "Language",
    "改动后需要重建索引": "Needs an index rebuild after changing",
    "{} · {} 维 · {}": "{} · {} dims · {}",
    "lite 模式不需要": "Not needed in lite mode",
    "{}  ({}/{} 文件)": "{}  ({}/{} files)",
    "{}  （空）": "{}  (empty)",
    "向量模型（embedding）": "Embedding model",
    "改后会实测维度；换模型需要重建数据目录":
        "Dims are measured after the change; switching models needs a new data dir",
    "运行模式 MEMORY_MODE": "Mode MEMORY_MODE",
    "lite 纯向量 / pro 加 LLM 抽取 / ultra 再加 Schema":
        "lite: vector only / pro: + LLM extraction / ultra: + schema",
    "pro 及以上用于抽取事实与自动打标签": "Used by pro and above for fact extraction and auto-tagging",
    "数据存储目录（storage）": "Storage directory",
    "查看用量、迁移、备份、清空重建": "Usage, migrate, backup, wipe and rebuild",
    "检索阈值 minScore": "Threshold minScore",
    "当前 {}": "Current {}",
    "队列参数": "Queue settings",
    "重试次数与锁超时，一般不用动": "Retries and lock timeout; rarely needed",
    "多智能体共享（通用）还是隔离（独立名字）":
        "Shared (general) across agents, or isolated under its own name",
    "完整向导": "Full wizard",
    "从向量模型开始重走一遍四步": "Walk through all four steps again from the embedding model",
    "查看当前配置": "View current config",
    "只读：所有生效值与环境变量映射": "Read-only: effective values and env mapping",
    "环境自检 doctor": "Doctor",
    "端到端连通性与依赖检查": "End-to-end connectivity and dependency checks",
    "界面语言 / Language": "Language / 界面语言",
    "当前 {}；切换后立即重绘菜单": "Current {}; the menu redraws immediately",
    "界面语言已切换为 {}，菜单文案立即生效。": "Language switched to {}; menu text applies immediately.",
    "环境变量 HY_MEM_LANG 覆盖了配置，界面语言仍是 {}；取消该变量后再切换":
        "HY_MEM_LANG overrides the config, so the UI stays {}; unset it to switch here",
    "切换失败：{}": "Switch failed: {}",

    # ---- 当前配置 / 首次设置 ----
    "首次设置": "First-run setup",
    "未完成": "Not done",
    "（来源：{}）": " (source: {})",
    "嵌入入口": "Embedding endpoint",
    "（本地 ONNX）": "(local ONNX)",
    "嵌入密钥": "Embedding key",
    "已设置": "Set",
    "未设置": "Not set",
    "LLM 密钥": "LLM key",
    "（来自 ${}）": " (from ${})",
    "重试 {} 次 / 锁超时 {}s": "retries {} / lock timeout {}s",
    "改这些走「设置」；直接改 config.json 也行，但下次运行才生效。":
        "Change these in Settings; editing config.json works too but applies on the next run.",

    # ---- 帮助页 ----
    "机器可读规格：hy-mem help --json （参数类型 / 默认值 / 返回字段 / 示例）":
        "Machine-readable spec: hy-mem help --json (types / defaults / fields / examples)",
    "身份切换：hy-mem user set <名字> —— 不设就是「通用」，多个智能体共享同一份记忆":
        "Identity: hy-mem user set <name> — unset means general, shared by all agents",
    "接入新宿主：hy-mem install —— 把启动器写到固定目录，让 hy-mem 在 PATH 上真实存在":
        "New host: hy-mem install — write the launcher to a fixed dir so hy-mem is on PATH",
    "按键自检：hy-mem tui-test —— 方向键没反应时先跑它":
        "Key self-test: hy-mem tui-test — run it first when arrow keys do nothing",

    # ---- 操作日志 ----
    "时间": "Time",
    "命令": "Command",
    "状态": "Status",
    "耗时": "Time",
    "摘要": "Summary",
    "共 {} 条（只保留最近 {} 条）；文件：{}": "{} entries (last {} kept); file: {}",
    "（还没有操作记录）": "(no operations recorded yet)",
    "清空日志": "Clear log",
    "删除全部记录，不可恢复": "Delete every record, irreversible",
    "清空全部 {} 条操作日志？": "Clear all {} log entries?",
    "日志已清空": "Log cleared",
    "清空失败：{}": "Clear failed: {}",
    "读取失败：{}": "Read failed: {}",

    # ---- 子进程 / 兜底 ----
    "无法启动子进程：{}": "Cannot start subprocess: {}",
    "子命令 30 分钟未结束，已放弃等待": "Subcommand did not finish in 30 minutes; gave up",
    "子进程启动失败：{}: {}": "Subprocess failed to start: {}: {}",

    # ---- errors.classify_error 的提示 ----
    "写入内容里有无法编码的字符（多为终端/管道不是 UTF-8）："
    "改用参数直传 `hy-mem add \"内容\"`，或先 `chcp 65001` 再管道":
        "The content has characters that cannot be encoded (the terminal or pipe is usually not UTF-8): "
        "pass it as an argument instead, i.e. `hy-mem add \"content\"`, or run `chcp 65001` first",
    "密钥或地址里含非 ASCII 字符（中文/全角/零宽），只能是 ASCII；"
    "重跑 hy-mem setup --step embedding 重新填一次":
        "The key or URL contains non-ASCII characters (CJK, full-width, zero-width); ASCII only — "
        "run hy-mem setup --step embedding and enter it again",
    "这多半是程序缺陷：加 --verbose 复跑可拿到堆栈":
        "This is most likely a bug: rerun with --verbose to get the stack trace",
    "向量库服务异常：先 `hy-mem serve` 看它的报错，或 `hy-mem doctor` 自检；"
    "确认无误后可以 `hy-mem queue` 观察后台任务是否卡住":
        "The vector store service is unhealthy: run `hy-mem serve` to see its error, or `hy-mem doctor`; "
        "then `hy-mem queue` to check whether background jobs are stuck",
    "先跑 `hy-mem doctor`；嵌入密钥无效用 `hy-mem setup --step embedding`":
        "Run `hy-mem doctor` first; for an invalid embedding key use `hy-mem setup --step embedding`",
    "加 --verbose 复跑可看到详细堆栈":
        "Rerun with --verbose to see the full stack trace",

    # ---- version / doctor / config 摘要 ----
    "源码": "source",
    "exe": "exe",
    "hy-mem {}（{}模式）": "hy-mem {} ({} mode)",
    "安装根 {}": "Install root {}",
    "数据目录 {}": "Data dir {}",
    "环境可用": "Environment OK",
    "环境不可用": "Environment NOT OK",
    "配置文件  {}": "Config file  {}",
    "  （尚未创建，运行 hy-mem setup）": "  (not created yet, run hy-mem setup)",
    "身份      {}   （hy-mem user 可切换）": "Identity     {}   (switch with hy-mem user)",
    "运行模式  {}": "Mode         {}",
    "向量模型  {}  ({} 维, {})": "Embedding    {}  ({} dims, {})",
    "嵌入入口  {}": "Embedding URL  {}",
    "嵌入密钥  {}": "Embedding key  {}",
    "LLM 模型  {} @ {}": "LLM model    {} @ {}",
    "LLM 密钥  {}": "LLM key      {}",
    "存储目录  {}": "Storage      {}",
    "检索阈值  {}": "Threshold    {}",
    "配置文件不存在，无需重置": "No config file, nothing to reset",
    "已删除 {}": "Deleted {}",
    "会删除 {}（含密钥）。确认请加 --yes": "This deletes {} (including keys). Pass --yes to confirm",
    "结论：配置完整": "Result: config is complete",
    "结论：{} 项未通过": "Result: {} checks failed",
    "已更新 {}": "Updated {}",
    "配置完整": "Config complete",

    # ---- 页面循环（写记忆、检索）：默认续用，空输入退出 ----
    "检索（第 {} 次）": "Search (#{})",

    # ---- 换嵌入模型后的数据目录处置 ----
    "数据目录怎么处理？": "What about the data directory?",
    "重建（备份后清空，推荐）": "Rebuild (backup then wipe, recommended)",
    "旧向量与新模型不兼容，混用会让检索失真": "Old vectors are incompatible with the new model; mixing them distorts search",
    "先不重建": "Keep it for now",
    "只改配置；旧向量仍在，检索可能失真，之后可在 设置 → 存储 里清空重建":
        "Config only; old vectors remain and search may be distorted — wipe & rebuild later in Settings → Storage",
    "放弃这次修改": "Discard this change",
    "不改配置，也不动数据": "Change nothing, touch no data",
    "环境变量里没有可用的 key（试过 {}），需要手工提供":
        "No usable key in env vars (tried {}), please enter one",

    # ---- 控制台（shell）：提示符 / 帮助 / 用法 ----
    "help 看命令 · Tab 补全 · ↑↓ 历史 · Ctrl+R 反查 · Ctrl+D 退出":
        "help for commands · Tab completes · ↑↓ history · Ctrl+R search · Ctrl+D exits",
    "↑↓ 历史 · Tab 补全 · Ctrl+R 反查 · Ctrl+C 放弃本行 · Ctrl+D 退出":
        "↑↓ history · Tab completes · Ctrl+R search · Ctrl+C drops the line · Ctrl+D exits",
    "hy-mem 命令": "hy-mem commands",
    "{} 用法": "{} usage",
    "别名：{}": "Aliases: {}",
    "可补全：{}": "Completes: {}",
    "按键": "Keys",
    "机器可读": "Machine-readable",
    "hy-mem help --json（参数类型 / 默认值 / 返回字段 / 示例）":
        "hy-mem help --json (arg types / defaults / response fields / examples)",
    "help <命令> 看单项用法，例如 help search": "help <command> for one command, e.g. help search",
    "完整命令清单：help ｜ 机器可读规格：hy-mem help --json":
        "Full command list: help ｜ machine-readable spec: hy-mem help --json",
    "输入 help 看全部命令；Tab 可以补全": "Type help for all commands; Tab completes",
    "未知命令 {}": "Unknown command {}",
    "是不是要 {}？": "Did you mean {}?",
    "没有这个命令：{}": "No such command: {}",
    "{}: {}": "{}: {}",
    "（已放弃这一行；exit 或 Ctrl+D 退出）":
        "(line dropped; exit or Ctrl+D to quit)",
    "退出。": "Bye.",
    "{} 后面要跟一个值": "{} needs a value",
    "不认识的选项 {}": "Unknown option {}",
    "例：search 部署流程 -n 10": "e.g. search deploy -n 10",
    "用法：search <查询> [-n 条数] [-t 标签]": "Usage: search <query> [-n count] [-t tags]",
    "用法：get <id>（id 从 list / search 的结果里取）":
        "Usage: get <id> (id comes from list / search)",
    "用法：delete <id>；批量删用 forget": "Usage: delete <id>; bulk delete with forget",
    "用法：tags rm <标签>": "Usage: tags rm <tag>",
    "用法：identity set <名字>（英文/数字/-/_ 最稳）":
        "Usage: identity set <name> (letters/digits/-/_ work best)",
    "用法：storage move <新目录>（支持 %USERPROFILE% 与 ~）":
        "Usage: storage move <new dir> (%USERPROFILE% and ~ supported)",
    "用法：lang zh|en|toggle": "Usage: lang zh|en|toggle",
    "可用：all / {}": "Available: all / {}",
    "可用：backup / move <路径> / reset / wipe / vacuum":
        "Available: backup / move <path> / reset / wipe / vacuum",
    "未知的设置项 {}": "Unknown setting {}",
    "未知的 storage 子命令 {}": "Unknown storage subcommand {}",
    "已取消，配置未改动。": "Cancelled; config unchanged.",
    "改这些用 setup <项>；直接改 config.json 也行，但下次运行才生效。":
        "Change these with setup <item>; editing config.json also works but needs a restart.",
    "共 {} 条；删除用 delete <id>": "{} total; delete with delete <id>",
    "共 {} 条（只保留最近 {} 条）": "{} total (only the latest {} kept)",
    "删除 {}？旧版本会一并消失": "Delete {}? Older versions go with it",
    "解绑所有 {} 标签？（不删记忆）": "Unbind every {} tag? (memories stay)",
    "同步自动标签：tags refresh ｜ 解绑：tags rm <标签>":
        "Sync auto tags: tags refresh ｜ unbind: tags rm <tag>",
    "消费：queue run（直到清空）/ queue one（只处理一条）/ queue retry（重投死信）":
        "Consume: queue run (until empty) / queue one (single task) / queue retry (redeliver dead)",
    "清空：log clear": "Clear: log clear",
    "备份：storage backup ｜ 迁目录：storage move <路径> ｜ 重建：storage wipe ｜ 清队列记录：storage vacuum ｜ 回默认：storage reset":
        "Backup: storage backup ｜ move: storage move <path> ｜ wipe: storage wipe ｜ "
        "queue cleanup: storage vacuum ｜ default: storage reset",
    "切身份：identity set <名字> ｜ 回通用：identity reset ｜ 看过哪些：identity list":
        "Switch: identity set <name> ｜ general: identity reset ｜ seen: identity list",
    "切换只改身份：模型配置、数据目录、已有记忆都不动":
        "Switching only changes identity: model config, data dir and memories stay",
    "（{} 维）": " ({} dims)",
    "→ 跑 setup（或 setup embedding）": "→ run setup (or setup embedding)",
    "  多行输入，空行结束；Ctrl+C 取消这条":
        "  Multiple lines; blank line ends it; Ctrl+C drops this one",
    "（没有输入内容，什么都没写）": "(nothing entered, nothing written)",
    "已入队，后台消费者会自动落库；此刻 search 还查不到是正常的（pro 每条约 10~50s，`queue` 看进度）":
        "Queued; the background consumer persists it. Not being searchable yet is normal "
        "(pro takes 10~50s per memory; watch with `queue`)",
    "可试着放宽：降低阈值（setup search）或换更接近原话的问法":
        "Try loosening it: lower the threshold (setup search) or rephrase closer to the original",
    "阈值 {}，命中 {} 条；看全文：get {}": "Threshold {}, {} hits; full text: get {}",
    "界面语言已切换为 {}，下一行提示就生效。":
        "UI language is now {}; the next prompt already reflects it.",
    "子命令": "Subcommands",
}
