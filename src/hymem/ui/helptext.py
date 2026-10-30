"""给智能体读的命令说明书 —— `hy-mem help --json` 的唯一数据源.

为什么要有这个模块：这个 CLI 的主要调用方不是人，是 agent。agent 不会去猜
`--min-score` 的默认值，也不会自己发现「多行文本必须走 stdin」。所以把
「有哪些命令、每个参数的类型与默认值、返回什么字段、什么时候该用哪个」
一次性输出成结构化 JSON，让 agent 一次读完就能正确调用。

两条不妥协的设计：

1. **参数表从 argparse 反射生成，不手写**。手写必然漂移：改了 `add` 的参数
   却忘了改文档，agent 就会按旧签名调用。反射保证了「help 说的」= 「能解析的」。

2. **语义从 CMD_META 手写**。反射能拿到类型，拿不到「什么时候用」「会改什么」。
   这部分是人类知识，必须写下来，且每条都带 examples —— agent 最需要的是可
   直接模仿的调用样例，而不是参数名词。

版本号也在这里，`hy-mem --version` 与 help 输出共用一份，避免两处不一致。
"""

from __future__ import annotations

import argparse

from hymem.core.version import VERSION  # noqa: F401  （S1：真源在 core，此处再导出保持兼容）

PURPOSE = (
    "跨会话长期记忆库。把用户偏好、项目事实、决策与踩坑写进去，下次会话靠语义检索取回。"
    "底层是腾讯 Hy-Memory SDK 2.0.5（chromadb），单机运行：向量库由 `hy-mem serve` "
    "长驻进程承载（数据唯一所有者），worker 只是队列消费者，CLI/UI/MCP 客户端经 "
    "HttpClient 访问。"
)

INVOCATION = {
    "packaged": "hy-mem.exe <子命令> [参数]",
    "source": "hy-mem.cmd <子命令> [参数]",
    "notes": [
        "多行文本一律走 stdin，不要当参数传：经 cmd.exe 时多行参数会被压平成一行。",
        "需要机器可解析输出时，把 --json 放在**子命令之前**（argparse 全局选项位置敏感）。",
        "无参数且非 TTY（被 agent 调用）时只打印帮助，不会卡住等输入。",
        "写操作建议 --async 入队，再单独跑 worker；pro 模式同步写单条 8~40s。",
    ],
}

EXIT_CODES = {
    "0": "成功",
    "1": "用法错误（参数不对、缺输入、需要 --yes 的破坏性操作未确认）",
    "2": "运行时错误（SDK 异常、端点不可达、密钥失效、维度不匹配等）；可修可重试。--json 时 stdout 给出 {ok:false,error:{code,message,hint}}",
    "3": "内部错误（程序自身缺陷，如 TypeError/KeyError）；加 --verbose 复跑拿堆栈，别浪费次数重试",
    "130": "被中断（Ctrl+C）",
}

HARD_RULES = [
    "MEMORY_EMBEDDING_DIMS 必须等于嵌入模型的实际维度；换模型必须清空数据目录重建，否则旧向量不可读。",
    "search 用复数 user_ids，add/list 用单数 user_id；混用会静默取不到数据。",
    "search 返回嵌套结构 memories.{normal,proactive,schema}，取结果前要拍平。",
    "list_memories 看不到 l1_raw 层；查最近写入要用 get_recent_history 兜底。",
    "memory_ids 是「本次影响到的节点」，不是「新建的节点」——它包含 reconcile 合并进来的旧节点。给新节点打标签必须取差集。",
    "SDK 内部没有文件锁，reconcile 是读改写；任何写入都必须先拿 store.WriteLock()，否则并发会丢更新。",
    "pro 模式写入延迟随库体积线性上升（reconcile 是 O(存量)），超过 30s 就该改用 --async。",
    "一条记忆只放一个主题。多主题合并成一条会被 reconcile 压成模糊摘要，检索时命中率显著下降。",
    "不要在非 TTY 环境调用 menu / setup：那两层是交互式的，会等待输入。",
    "不用传 -u/--user：默认身份来自 config.identity.userId 或 HY_MEM_USER_ID，都没有就是「通用（default）」。通用身份下所有智能体共享同一份记忆；要给某个智能体单独隔离，用 `hy-mem user set <名字>`，切换只改身份、不动数据。search 空结果前会自证端点，端点挂了会以 exit 2 报错，而不是「没有相关记忆」。",
    "配置不全时命令会以 exit 1 + error.code=E_CONFIG 返回，error.missing 列出缺项与修复命令；不要把它当「没有数据」，先补齐配置再重试。",
    "删除类命令（delete/forget/untag 全局/retry）默认需要 --yes，不要替用户猜；拿不准先用 --dry-run 看影响面（delete/forget 都支持）。",
    "全局选项可以放在命令行的任意位置：--config / --json / --fields 由入口先扫描剥离，不必挤在子命令前面。",
]

WORKFLOWS = [
    {
        "name": "记住一件事（推荐路径）",
        "steps": [
            'hy-mem --json -u <user> add --async --tag <tag> "一条一个主题的事实"',
            "hy-mem worker --once    # 或让常驻 worker 消费",
            "hy-mem --json queue      # 确认 pending 归零、dead 为 0",
        ],
    },
    {
        "name": "回答前先回忆",
        "steps": [
            'hy-mem --json -u <user> search "用户上次说的部署方式" -n 5',
            "解析 results[].content 与 score；无命中就正常回答，不要编造记忆内容",
        ],
    },
    {
        "name": "按类归档",
        "steps": [
            "hy-mem --json add --tag infra --tag k8s \"...\"",
            "hy-mem --json tags                 # 看各标签条数",
            'hy-mem --json search "..." --tag infra      # 只在某类里找',
            "hy-mem --json archive --by layer   # 看层分布，决定要收缩哪部分",
        ],
    },
    {
        "name": "环境出问题时",
        "steps": [
            "hy-mem --json doctor               # 一条命令判定环境是否可用",
            "hy-mem --json config validate      # 静态校验配置项",
            "hy-mem --json stats --health       # 体积/条数/延迟预期/死信",
        ],
    },
]

INTERACTIVE_KEYS = {
    "up/down 或 k/j": "移动光标",
    "英文热键（[e]/[w]/[r]…）": "按下即选中对应项，不必先移动光标；标签里方括号内的字母就是热键",
    "行编辑 ←→ / Home / End": "输入框内移动光标",
    "行编辑 退格 / Delete": "删字符",
    "行编辑 Ctrl+U / Ctrl+W": "清空整行 / 删一个词",
    "Home/End 或 g/G": "跳到首/尾项",
    "PgUp/PgDn": "翻 5 项",
    "Enter/Space/→": "选中当前项",
    "Esc/←/q": "返回上一层（主界面即退出）",
    "1~9": "直达第 n 项",
    "Ctrl+C": "中断当前操作，不写入任何改动",
    "鼠标拖选 / 右键复制": "用终端自己的选择与复制（默认就可用）；Ctrl+C 是中断，别拿它复制",
    "鼠标点选": "只有设了 HY_MEM_MOUSE=1 才启用（会抢占终端的原生选择）",
}

ENV_VARS = [
    ("MEMORY_MODE", "lite|pro|ultra", "运行模式；config.json 优先"),
    ("MEMORY_DATA_DIR", "路径", "记忆库与队列库所在目录"),
    ("MEMORY_EMBEDDER_PROVIDER", "openai|local", "嵌入来源"),
    ("MEMORY_EMBEDDER_BASE_URL", "URL", "OpenAI 兼容入口，通常以 /v1 结尾"),
    ("MEMORY_EMBEDDER_MODEL", "模型名", "嵌入模型"),
    ("MEMORY_EMBEDDER_API_KEY", "字符串", "嵌入密钥（也可用 config.json）"),
    ("MEMORY_EMBEDDING_DIMS", "整数", "必须等于实际维度"),
    ("MEMORY_LOCAL_EMBED_MODEL", "模型名", "provider=local 时的 ONNX 模型"),
    ("MEMORY_LLM_BASE_URL", "URL", "LLM 入口"),
    ("MEMORY_LLM_MODEL", "模型名", "LLM 模型"),
    ("MEMORY_LLM_API_KEY", "字符串", "LLM 密钥；常由 YSJX_API_KEY 继承"),
    ("HY_MEM_MIN_SCORE", "0~1", "检索相似度下限，默认 0.35"),
    ("HY_MEM_USER_ID", "字符串", "默认 user_id；覆盖 config.identity.userId，省掉每条命令的 -u"),
    ("HY_MEM_DATA_DIR", "路径", "覆盖默认数据目录（默认是安装根下的 data）"),
    ("HY_MEM_NO_PROBE", "1", "为空结果时的嵌入端点自证探测（默认开，约 300ms）"),
    ("HY_MEM_ASYNC", "1", "设为 1 时 add 默认走队列"),
    ("HY_MEM_TUI", "0", "关掉方向键交互，回到编号菜单"),
    ("HY_MEM_LANG", "zh|en", "界面语言（交互界面与常用命令文案）；覆盖 config.json 的 lang"),
    ("HY_MEM_MOUSE", "1", "启用鼠标上报（默认关；开着会抢走终端的拖选与右键复制）"),
    ("HY_MEM_CONFIG", "路径", "覆盖 config.json 位置"),
    ("HY_MEM_HOME", "路径", "覆盖安装根目录"),
]

# 每条命令的「人话部分」：什么时候用、会不会改数据、返回什么、有哪些坑
GROUP_ORDER = ["元信息", "交互", "配置", "写入", "读取", "队列", "标签", "诊断", "其它"]

# 每条命令的「人话部分」：什么时候用、会不会改数据、返回什么、有哪些坑
CMD_META: dict[str, dict] = {
    "help": {
        "group": "元信息",
        "when": "不确定某个命令怎么调，或第一次接入时。agent 应优先读 help --json。",
        "mutates": False,
        "examples": ["hy-mem help", "hy-mem help --json", "hy-mem help search", "hy-mem help search --json"],
        "output": {"HELP-SPEC": "见本文件 build_spec() 结构：commands/global_options/env/exit_codes/hard_rules/workflows"},
        "notes": ["--json 输出整份规格；带命令名时只输出该命令。"],
    },
    "menu": {
        "group": "交互",
        "when": "人类直接使用。agent 不要调用。",
        "mutates": True,
        "examples": ["hy-mem"],
        "output": {"n/a": "交互界面，无结构化输出"},
        "notes": ["非 TTY 下不会进入，只会打印帮助。"],
    },
    "setup": {
        "group": "交互",
        "when": "首次配置，或需要改向量模型/模式/LLM/沉淀模型/存储目录时。agent 不要调用。",
        "mutates": True,
        "examples": [
            "hy-mem setup",
            "hy-mem setup --step embedding",
            "hy-mem setup --step evolve",
            "hy-mem setup --step storage",
        ],
        "output": {"n/a": "写入 config.json 并打印自检结果"},
        "notes": [
            "改嵌入模型会实测维度，并提示备份重建数据目录（旧向量与新模型不兼容）。",
            "「沉淀模型」= L6 归纳（evolve）用哪个模型，**只对 ultra 生效**；只能填与 LLM 同一端点下的另一个模型名，端点/密钥仍共用 LLM 那套。",
        ],
    },
    "skill": {
        "group": "元信息",
        "when": "接入新宿主、或怀疑「宿主没看到帮助」时：确认 SKILL.md 在哪、各宿主副本是否同步。",
        "mutates": "只读；带 --install / --dir 时才写文件（复制 SKILL.md）",
        "examples": [
            "hy-mem skill",
            "hy-mem --json skill",
            "hy-mem skill --install",
            "hy-mem skill --dir ~/.workbuddy/skills",
            "hy-mem skill --print",
        ],
        "output": {
            "source": "SKILL.md 绝对路径",
            "md5": "内容指纹",
            "hosts": "[{host, dir, exists, inSync}] 各宿主副本状态",
        },
        "notes": [
            "命令在 PATH 上 + 宿主能读到 SKILL.md，两者都成立才算「接好了」；install 管前者，skill 管后者。",
            "inSync=false 说明宿主持有的是旧版，跑 --install 覆盖即可。",
        ],
    },
    "install": {
        "group": "元信息",
        "when": "接入新宿主时先跑一次：把启动器写到固定目录，让 `hy-mem` 这条命令在 PATH 上真实存在。之后跑 `hy-mem skill --install` 把 SKILL.md 铺到宿主。",
        "mutates": "写入一个 .cmd / shell 启动器（--print 时只打印不落盘）",
        "examples": ["hy-mem install", "hy-mem --json install", "hy-mem install --print"],
        "output": {"ok": "布尔", "launcher": "启动器绝对路径", "onPath": "父目录是否已在 PATH", "pathHint": "不在 PATH 时给出的命令"},
        "notes": [
            "默认落点：Windows %LOCALAPPDATA%\\\\hy-mem\\\\bin，其它平台 ~/.local/bin。",
            "不改 PATH，只告诉你该加哪一条——改环境变量这件事不替用户做决定。",
        ],
    },
    "version": {
        "group": "元信息",
        "when": "自动升级前比对版本号，或需要知道安装根 / 数据目录在哪时。",
        "mutates": False,
        "examples": ["hy-mem version", "hy-mem version --json"],
        "output": {
            "version": "当前版本（与 --version 同源：helptext.VERSION）",
            "mode": "exe | source",
            "installRoot": "安装根绝对路径——升级就是替换这个目录",
            "configPath": "生效配置的路径（HY_MEM_CONFIG 优先）",
            "configured": "是否已有配置（false 时走的是默认值）",
            "dataDir": "当前生效的数据目录",
        },
        "notes": ["与 --version 输出同一份版本号，但附带定位信息；自动升级流程：version --json → 下载新版 → 替换 installRoot。"],
    },
    "migrate": {
        "group": "配置",
        "when": "升级接管后数据还在旧安装目录时：把数据迁入当前安装根，之后才能安全删除旧目录。",
        "mutates": "复制数据目录到 <安装根>/data 并改写 config.json 的 storage.dataDir（原目录保留）",
        "examples": ["hy-mem migrate", "hy-mem migrate --json"],
        "output": {
            "migrated": "是否执行了迁移（数据已在当前安装内时为 false）",
            "from": "原数据目录",
            "to": "新数据目录",
            "bytes": "迁移的数据量（字节）",
        },
        "notes": [
            "worker 运行中会拒绝迁移（否则新旧目录数据分家）；等它空闲自退后再跑。",
            "原目录不删，留作备份；目标目录非空时拒绝执行。",
        ],
    },
    "tui-test": {
        "group": "交互",
        "when": "人类诊断用：方向键/鼠标没反应时，先跑它确认终端把按键发成了什么。agent 不要调用。",
        "mutates": False,
        "examples": ["hy-mem tui-test"],
        "output": {"stream": "逐行打印解码后的按键名（up/down/char:x/mouse:...）"},
        "notes": ["只能在真实终端里用；非 TTY 会直接返回错误提示。"],
    },
    "config": {
        "group": "配置",
        "when": "读当前生效配置、静态校验、或脚本化改某一项。",
        "mutates": "set/reset 会改 config.json",
        "examples": [
            "hy-mem --json config",
            "hy-mem --json config validate",
            "hy-mem config set search.minScore=0.4",
            "hy-mem config reset --yes",
        ],
        "output": {
            "config": "{ok, path, exists, mode, embedding{model,dims,provider,apiKeySet}, llm{...keySet}, dataDir, minScore}",
            "validate": "{ok, checks:[{name,pass,detail}]}",
        },
        "notes": ["set 用点路径，例如 embedding.model=xxx；改嵌入相关字段后要自己确认维度一致。",
                  "界面语言：hy-mem config set lang=en（或设置页的「界面语言」），立即生效。"],
    },
    "user": {
        "group": "配置",
        "when": "多智能体场景下决定「共享还是隔离记忆」：不设 user_id 用通用身份（共享），设了就自成一套（隔离）。只想换身份、不想动模型和数据目录时用它。",
        "mutates": "set / reset 会改 config.json 的 identity.userId（只改这一项）",
        "examples": [
            "hy-mem --json user",
            "hy-mem user set writer",
            "hy-mem user reset",
            "hy-mem --json user list",
            'hy-mem -u writer search "..."',
        ],
        "output": {
            "show": "{ok, userId, general, label, source(env|config|general), configured, envOverride, known, configPath}",
            "set/reset": "{ok, userId, general, previous, path, envOverride?}",
            "list": "{ok, current, users:[{userId,label,current}]}",
        },
        "notes": [
            "优先级：-u 参数 > 环境变量 HY_MEM_USER_ID > config.json identity.userId > 通用（default）。",
            "切换不会删除任何记忆：旧身份的数据只是当前看不到，切回去仍在。",
            "agent 通常不需要传 -u；需要给某个智能体单独一份记忆时才显式 set。",
        ],
    },
    "doctor": {
        "group": "诊断",
        "when": "任何异常排查的第一步；也适合在会话开始做一次健康确认。",
        "mutates": False,
        "examples": ["hy-mem --json doctor", "hy-mem --json doctor --data-dir D:/hy-mem/data"],
        "output": {"report": "{ok, checks:[{name, ok, detail, level}]}"},
        "notes": ["会真发一次嵌入与 LLM 请求（耗时数秒）。全 PASS 才算环境可用。"],
    },
    "add": {
        "group": "写入",
        "when": "用户说「记一下」、表达偏好/决策/项目事实、确认了一个技术方案时。写事实型内容（主体+具体值），别写时间性流水——事实可复用，流水检索价值低。",
        "mutates": True,
        "examples": [
            'hy-mem add "招商活动报名审批流用自研 FSM，fireAndPersist 落库"',
            'hy-mem add --tag infra --tag k8s "生产集群 12 节点，etcd 独立部署"',
            'hy-mem add --async --split < facts.txt',
            'cat notes.md | hy-mem add --split',
            'hy-mem add --scene coding --workspace github.com/me/repo --branch master --trace trace.json',
        ],
        "output": {
            "single": "{ok, memory_id, memory_ids[], tagged_ids[], tags[], ...SDK 原始字段}",
            "split": "{ok, count, memory_ids[], tagged_ids[], tags[]}",
            "async": "{ok, queued, job_ids[], tags[], queue{pending,running,done,dead}}",
            "scene": "{ok, scene, request_id, fact_ids[], experience_ids[], chain_rounds, workspace, branch}",
        },
        "notes": [
            "一条一个主题；--split 按行拆条，显著提升检索命中率。",
            "同步 add 两段式：LLM 抽取在锁外（可多进程并行），写锁只包 SDK 标准提交段（约 3~15s）；抢不到写锁 5s 即失败（E_LOCK，本条零写入），超时可用 HY_MEM_ADD_LOCK_TIMEOUT 调整。",
            "多条/并发务必 --async：入队即返回，3 车道消费；批量写法 --async --split。",
            "标签绑到「本次真正新建」的节点（已做差集），不会污染旧节点。",
            "--scene working|coding 是独立管线（独立库、独立向量集合），与 chat 记忆互不可见，检索要带同样的 --scene。必须配 --trace（OpenAI messages 数组，含 tool_calls/role=tool 的工具轨迹）；没有轨迹会被拒（E_NO_TRACE）——SDK 对无轨迹的显式 scene 会静默回退 chat，这里在门口拦住。",
            "coding 只记「文件坐标 + 决策」，不要贴整段代码：抽取预算约 200 字，代码放上去会把最有价值的坐标挤掉。想留 diff 版本链，就把 `git diff` 输出放进 tool 消息的 content（链层从文本里读 diff，读不到 assistant 的 tool_calls.arguments）。",
            "--scene 写入恒同步（不走异步队列），返回即已落库。",
        ],
    },
    "search": {
        "group": "读取",
        "when": "主题类问题：历史偏好/项目上下文/技术事实（「部署目录在哪」「用的什么模型」）。时间类问题（「我昨天做了什么」）别用 search——语义匹配对时间问法必空手，改 list --since/--until。",
        "mutates": False,
        "examples": [
            'hy-mem --json search "上次定的部署方式" -n 5',
            'hy-mem --json search "网关限流" --tag infra',
            'hy-mem --json search "报名审批" --min-score 0.28',
            'hy-mem --json search "请求超时怎么修的" --scene coding --workspace github.com/me/repo --chain-block',
            'hy-mem search "这套系统在持久化上有什么讲究" --schema-limit 3',
        ],
        "output": {
            "resp": "{count, min_score, elapsed_ms, tags_filter[], results:[{score, content, layer, tags}], schemas[], schema_available, schema_note?}",
            "scene": "{scene, count, facts[], experiences[], chain_block, chain_used[], chain_note?}",
        },
        "notes": [
            "默认阈值 0.35，是实测最优分割点；改写问法分数带窄，漏召回时先降到 0.30 再试。",
            "标签是后过滤，命中标签时会自动多取候选，所以 --tag 与 -n 可同时用。",
            "无命中是正常结果，不要据此编造记忆内容。",
            "--scene working|coding 搜生产力/编码库（facts 事实段 + experiences 经验段两路），默认只搜 chat。两库互不覆盖，跨库要搜两次。",
            "--chain-block 只在 --scene coding 生效：额外返回该文件的 diff 版本演进块，默认关（注入 diff 会明显加长返回体）。",
            "带 --workspace 检索时，若 chain_block 由 hy-mem 兜底补齐，返回里会有 chain_note 说明（上游 SDK 在 workspace 作用域下会把链全滤掉）。",
            "schemas 是 L6 行为模式（「这人做事的一贯方式」），仅 ultra 模式且跑过 evolve 才有；pro 下恒为空并带 schema_note 说明。",
            "schemas 独立成段：不占 -n、不混进 results、按语义相似度排序（与普通结果的分数同一尺度，可直接比较）。默认 3 条，--no-schema 可关。",
        ],
    },
    "list": {
        "group": "读取",
        "when": "时间类问题的正解（「我昨天做了什么」→ --since 1d 浏览）；也用于浏览最近写入、拿 id 去删。",
        "mutates": False,
        "examples": ["hy-mem --json list -n 20", "hy-mem --json list --tag infra --offset 20"],
        "output": {"resp": "{count, source(vdb|recent), tags_filter[], memories[]}"},
        "notes": ["lite 模式下 l1_raw 不在 vdb 列表里，会自动回退 get_recent_history。"],
    },
    "get": {
        "group": "读取",
        "when": "已知 memory_id，要看完整内容与元数据。",
        "mutates": False,
        "examples": ["hy-mem --json get 3f9a2c1b"],
        "output": {"resp": "{memory_id, content, layer, tags[], create_time, ...}"},
        "notes": ["id 不存在返回 exit 2。"],
    },
    "outcome": {
        "group": "读取",
        "when": "手里有 request_id（add / add --scene 返回的那个），想看 SDK 最终写成了什么。比 queue_status 更进一步——queue_status 看的是 job 状态，这里看的是结果本身。",
        "mutates": False,
        "examples": ["hy-mem --json outcome 6fc9354b-6e8c-4bca"],
        "output": {"resp": "{scene, fact_ids[], experience_ids[], ops_summary, ...SDK 原样}"},
        "notes": [
            "request_id 只在写入时带了（或 SDK 自动生成）才有；查不到就说明仍在写入或 id 不对。",
            "异步队列的 job 状态用 queue_status，两者不是一个维度。",
        ],
    },
    "profile": {
        "group": "读取",
        "when": "想知道「记忆里的这个人是谁」——姓名/职业/所在地这类 L0 基础信息，与检索出来的 L1 事实是两层。",
        "mutates": False,
        "examples": ["hy-mem profile", "hy-mem --json profile"],
        "output": "{user, count, memories[], elapsed_ms}",
        "notes": [
            "L0 basic_info 是 add 时顺带抽取的，零 LLM/零 embedding，读取很快。",
            "空结果说明还没有自述类记忆——add 一条「我叫 X，在 Y 做 Z」即可。",
            "身份由 -u / HY_MEM_USER_ID / config.identity.userId 决定，与 search 同口径。",
        ],
    },
    "evolve": {
        "group": "维护",
        "when": "想把一堆零散事实归纳成「行为模式」（L6）。add 只跑 S1（抽事实/判重），不会自动归纳；evolve 扫当前全量 VDB 做一次 System 2。",
        "mutates": True,
        "examples": [
            "hy-mem evolve",
            "hy-mem evolve --model deepseek-v4.1-flash-1017",
            "hy-mem --json evolve --batches 2",
            "hy-mem config set evolve.model=deepseek-v4.1-flash-1017",
        ],
        "output": "{ok, idle, batches_completed, tasks_processed, prompt_tokens, completion_tokens, schemas_before, schemas_after, schemas_added, model, note?, idle_hint?}",
        "notes": [
            "仅 ultra 可用。pro/lite 下直接拒（exit 1），因为 schema 要落 Kuzu 图库，这两个模式没有这条存储。切换：hy-mem config set mode=ultra。",
            "非幂等：每次扫当前全量 VDB，add 一批再 evolve 一次才有新意义。",
            "素材要求「同主题、细节各异、≥4 条且没被 reconcile 合并」。太散不成簇（LLM 根本不会被调用），太像会被判重合并成一条。",
            "同一批事实只会被归纳一次：参与过的会被标成已用（evidenced_excluded），所以「用弱模型跑空转了，再换强模型重跑」通常仍然没产出——要么 add 新内容，要么一开始就用 --model 指定强模型。",
            "模型选择很关键：意图分类模型（如 tongyi-*-analysis-flash）抽取很快但做不了开放归纳，实测 3850 prompt tokens 只回 7 个 token。这种空转会被标成 idle=true，框架仍报 success——别被 success 骗了。",
            "kuzu 是独占锁：MCP 服务长驻会一直持有图库，CLI 的 evolve 会失败。先停 MCP，或改用 MCP 里的 evolve 工具（跑在持有进程内）。",
            "--model 只影响本次，不改 config.json；想长期生效写 evolve.model。",
        ],
    },
    "delete": {
        "notes": [
            "不可恢复。拿不准先加 --dry-run 看一眼要删的东西。",
            "与 forget 的区别：delete 按 id 精确删一条，forget 按 user/agent/session 批量清。",
            "会级联清理该记忆的 l2 派生节点（上游 SDK 不级联，hy-mem 侧补扫）。",
            "单条删除不需要 --yes；批量删除用 forget（需要 --yes）。删前确认。",
        ],
        "group": "写入",
        "when": "用户明确要求删掉某条记忆。",
        "mutates": True,
        "examples": ["hy-mem delete 3f9a2c1b"],
        "output": {"resp": "{success, deleted_count, derived_purged[], ...}"},
    },
    "forget": {
        "group": "写入",
        "when": "清理某用户/某会话的全部记忆，例如测试残留。",
        "mutates": True,
        "examples": ["hy-mem forget --yes -u test_probe"],
        "output": {"resp": "{ok, removed}"},
        "notes": ["破坏性操作，必须 --yes；范围由 -u/-a/-s 决定，建议先 list 确认范围。"],
    },
    "update": {
        "group": "写入",
        "when": "记忆内容有误或过时、用户纠正了某个事实时；新增信息用 add，别用 update。",
        "mutates": True,
        "examples": ["hy-mem update 3f9a2c1b 用户的项目已切换到 iisw_be2", "echo 新内容 | hy-mem update 3f9a2c1b"],
        "output": {"resp": "{ok, ...SDK 返回}"},
        "notes": ["整条替换正文，不是追加。走演化链：旧版本进 SHADOW 保留，hy-mem history <id> 可查。"],
    },
    "history": {
        "group": "读取",
        "when": "审计一条记忆被改过几次、原来的内容是什么。",
        "mutates": False,
        "examples": ["hy-mem --json history 3f9a2c1b"],
        "output": {"resp": "{count, memory_id, history[]}"},
        "notes": ["没有变更记录时返回空列表（说明从没被 update 过）。"],
    },
    "finalize": {
        "group": "写入",
        "when": "会话结束、需要触发一次演化归纳时。",
        "mutates": True,
        "examples": ["hy-mem --json finalize"],
        "output": {"resp": "{ok, ...SDK 返回}"},
        "notes": ["实测在本项目配置下可能是空操作（2s、无新增事实），不要依赖它做批量整合。"],
    },
    "stats": {
        "group": "诊断",
        "when": "看体积/条数/写入延迟预期；--health 是「库要不要收缩」的唯一入口。",
        "mutates": "--vacuum 会清理 jobs 表",
        "examples": ["hy-mem --json stats", "hy-mem --json stats --health", "hy-mem --json stats --health --vacuum"],
        "output": {
            "metrics": "SDK get_metrics 原始结构",
            "health": "{ok, verdict(OK|WARN|CRIT), memory_count, layers{}, size_mb, estimated_write_latency_s, queue{}, top_tags[], checks[], advice[]}",
        },
        "notes": ["pro 写入延迟随条数上升，verdict=CRIT 时应改 --async 或归档收缩。"],
    },
    "serve": {
        "group": "队列",
        "when": "起向量库服务（chroma server）。客户端会按需自动拉起，一般不用手跑；排查端点问题或想常驻时用。",
        "mutates": False,
        "examples": ["hy-mem serve", "hy-mem serve --idle-exit 300", "hy-mem --json serve"],
        "output": {"resp": "{ok, already(bool), port, pid, reason, uptime_s}"},
        "notes": [
            "数据唯一所有者：同一数据目录只允许一个 serve，重复启动会立即退出（already=true）。",
            "空闲 --idle-exit 秒（默认 1800）内没有请求就自行退出；退出只删自己登记的端口文件。",
            "worker 是纯消费者，不会替代 serve（0.9.9 的端点事故根因就是把两者塞进一个进程）。",
        ],
    },
    "worker": {
        "group": "队列",
        "when": "消费 --async 入队的任务。agent 可在后台跑一次 --once。",
        "mutates": True,
        "examples": ["hy-mem worker --once", "hy-mem worker --idle-exit 60", "hy-mem worker --max 3"],
        "output": {"resp": "{ok, processed, succeeded, failed, queue{}}", "stream": "每个任务一行 JSON（--json 时）"},
        "notes": [
            "单写者：同时只应有一个 worker 在跑（内部用 WriteLock 与 SQLite 抢占兜底）。",
            "--once 处理完当前队列即退出，适合 agent 调用；常驻模式给人用。",
            "worker 永不起 chroma server；需要端点时会自动拉起 serve。",
        ],
    },
    "queue": {
        "group": "队列",
        "when": "看积压、卡住的任务、死信。",
        "mutates": "--recover 会把僵死任务放回待处理",
        "examples": ["hy-mem --json queue", "hy-mem --json queue --status dead", "hy-mem --json queue --recover"],
        "output": {"resp": "{ok, stats{pending,running,done,dead,oldest_pending_age_s}, jobs[]}"},
        "notes": ["death 通常意味着 LLM 或嵌入端点持续失败，先 doctor 再 retry。"],
    },
    "drain": {
        "group": "队列",
        "when": "写完一批后阻塞等到全部落库（脚本/收尾用）。",
        "mutates": False,
        "examples": ["hy-mem --json drain --timeout 600"],
        "output": {"resp": "{ok, queue{}}"},
        "notes": ["不要与常驻 worker 同时依赖：drain 只等待，不消费。"],
    },
    "mcp": {
        "group": "接入",
        "when": "宿主（WorkBuddy/CodeBuddy 等）配 MCP 时用：mcp.json 里 command=hy-mem, args=[mcp]。由宿主按会话拉起，勿手动运行。",
        "mutates": True,
        "examples": ['mcp.json → {"mcpServers":{"hy-mem":{"command":"hy-mem","args":["mcp"]}}}'],
        "output": {"resp": "JSON-RPC 2.0（stdio 换行分隔）；tools: add_memory/queue_status/search_memory/list_memories/get_memory/update_memory/delete_memory/get_version"},
        "notes": [
            "stdout 只准有协议响应，诊断走 stderr。",
            "身份固定为默认（config 的 identity.userId），与 CLI 不传 -u 同口径。",
        ],
    },
    "retry": {
        "group": "队列",
        "when": "死信任务修复后重投。",
        "mutates": True,
        "examples": ["hy-mem --json retry dead", "hy-mem --json retry job_ab12", "hy-mem --json retry --shadow"],
        "output": {"resp": "{ok, requeued, queue{}}"},
        "notes": [
            "retry dead 会重投全部死信，量大时先 queue --status dead 看原因。",
            "retry --shadow 重投「完成任务但只落了 l1_raw 影子、检索不到」的那些（queue 里 shadow_only 计数）。",
        ],
    },
    "tags": {
        "group": "标签",
        "when": "看分类分布；--refresh 把 SDK 侧 LLM 自动标签同步进本地索引。",
        "mutates": "--refresh 会写本地标签索引",
        "examples": ["hy-mem --json tags", "hy-mem --json tags --refresh", "hy-mem --json tags --min-count 2"],
        "output": {"resp": "{ok, count, tags:[{tag,n,user_n,auto_n}], refreshed, pruned}"},
        "notes": ["检索用 --tag 过滤，不走 SDK 查询条件，是后过滤。"],
    },
    "untag": {
        "group": "标签",
        "when": "解绑标签（不删记忆）。",
        "mutates": True,
        "examples": ["hy-mem untag 3f9a2c1b --tag infra", "hy-mem untag --tag infra --yes"],
        "output": {"resp": "{ok, removed}"},
        "notes": ["不带 memory_id 时是全局解绑，必须 --yes。"],
    },
    "archive": {
        "group": "标签",
        "when": "按标签或记忆层盘点，决定收缩策略。",
        "mutates": False,
        "examples": ["hy-mem --json archive --by tag", "hy-mem --json archive --by layer"],
        "output": {"resp": "{ok, by, groups:[{key, n}]}"},
        "notes": ["条数接近阈值（默认 200/500）时，reconcile 开销会拖慢写入。"],
    },
    "log": {
        "group": "诊断",
        "when": "回看「刚才那条命令到底跑了什么、为什么失败」——尤其是交互界面或 worker 里发生的失败，终端滚动缓冲早就没了。",
        "mutates": "--clear 会清空日志文件",
        "examples": ["hy-mem log", "hy-mem log -n 10", "hy-mem --json log -n 5", "hy-mem log --clear"],
        "output": {
            "entries": "[{time, cmd, status, duration, summary}] 最旧在前",
            "path": "ops.log 绝对路径（在数据目录下）",
            "keep": "只保留最近多少条（固定 50）",
        },
        "notes": [
            "只保留最近 50 条，追加时自动截断；密钥类参数（apiKey/token/secret/password）打码为 ***。",
            "add/update/search 的正文与查询词刻意不记录——日志不该成为记忆内容的第二份明文副本。",
        ],
    },
}


def _opt_entry(action: argparse.Action) -> dict:
    e: dict = {}
    if action.option_strings:
        e["flag"] = " / ".join(action.option_strings)
    e["dest"] = action.dest
    t = getattr(action, "type", None)
    if t in (int, float, str):
        e["type"] = t.__name__
    if isinstance(action.choices, (list, tuple)):
        e["choices"] = list(action.choices)
    if isinstance(action, argparse._StoreTrueAction):
        e["kind"] = "flag"
    elif isinstance(action, argparse._AppendAction):
        e["kind"] = "append"
    else:
        e["kind"] = "value"
    if e["kind"] != "flag" and "type" not in e:
        # argparse 里没显式写 type= 的选项就是 str；不补这一项，agent 会以为是
        # 任意类型，遇到数字/布尔还得试错。
        e["type"] = "str"
    if action.nargs is not None and e["kind"] != "flag":
        e["nargs"] = action.nargs if isinstance(action.nargs, str) else str(action.nargs)
    if action.option_strings:
        e["required"] = False
        # 空字符串 / None / 布尔默认值不写进规格：它们对调用方没有信息量，
        # 只会渲染成「（默认 ）」这种噪声，还把同一行挤到折行。
        if (
            action.default not in (None, "", argparse.SUPPRESS)
            and not isinstance(action.default, bool)
        ):
            e["default"] = action.default
    else:
        e["positional"] = True
        e["required"] = action.nargs not in ("?", "*") and action.default is None
    if action.help and action.help is not argparse.SUPPRESS:
        e["help"] = action.help
    return e


def build_spec(parser: argparse.ArgumentParser) -> dict:
    """从 argparse 反射出参数表，再叠加 CMD_META 的语义。"""
    globals_ = [_opt_entry(a) for a in parser._actions if a.option_strings and a.dest != "help"]
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    commands = []
    if subs:
        group = subs[0]
        # 子命令的 help 摘要挂在 subparsers action 的 _choices_actions 上（不是子解析器自己）
        summaries = {}
        for ca in getattr(group, "_choices_actions", []):
            dest = getattr(ca, "dest", "")
            if dest:
                summaries[dest.split(" ")[0]] = ca.help or ""
        for name, sp in group.choices.items():
            summary = summaries.get(name, "")
            args, opts = [], []
            for a in sp._actions:
                if a.dest == "help" and a.option_strings:
                    continue
                if isinstance(a, argparse._SubParsersAction):
                    continue
                (opts if a.option_strings else args).append(_opt_entry(a))
            meta = CMD_META.get(name, {})
            commands.append(
                {
                    "name": name,
                    "group": meta.get("group", "其它"),
                    "summary": summary,
                    "when": meta.get("when", ""),
                    "mutates": meta.get("mutates", None),
                    "args": args,
                    "options": opts,
                    "examples": meta.get("examples", []),
                    "output": meta.get("output", {}),
                    "notes": meta.get("notes", []),
                }
            )
    order = list(CMD_META)
    commands.sort(
        key=lambda c: (
            GROUP_ORDER.index(c["group"]) if c["group"] in GROUP_ORDER else 99,
            order.index(c["name"]) if c["name"] in order else 999,
        )
    )
    return {
        "tool": "hy-mem",
        "version": VERSION,
        "purpose": PURPOSE,
        "invocation": INVOCATION,
        "global_options": globals_,
        "commands": commands,
        "interactive_keys": INTERACTIVE_KEYS,
        "env": [{"name": n, "example": v, "note": d} for n, v, d in ENV_VARS],
        "exit_codes": EXIT_CODES,
        "hard_rules": HARD_RULES,
        "workflows": WORKFLOWS,
        "agent_tips": [
            "调用前先确定用户身份：绝大多数命令都应带 -u <user_id>。",
            "取结果只信 --json 的 stdout；stderr 是给人看的错误摘要。",
            "写入成功不等于已落库：--async 只表示入队，需要 worker 消费。",
            "一次检索没命中就换个更贴近原话的问法，或降 --min-score 到 0.30 再试一次。",
        ],
    }


def render_text(spec: dict, cmd: str | None = None) -> str:
    """人类可读版。刻意保持简洁，细节留给 --json。"""
    if cmd:
        one = next((c for c in spec["commands"] if c["name"] == cmd), None)
        if one is None:
            return f"没有这个命令：{cmd}"
        lines = [f"{spec['tool']} {one['name']}  —  {one['summary']}", ""]
        if one.get("when"):
            lines.append(f"何时用：{one['when']}")
        if one.get("mutates") is not None:
            lines.append(f"会改数据：{'是' if one['mutates'] is True else (one['mutates'] or '否')}")
        lines.append("")
        if one["args"]:
            lines.append("位置参数：")
            for a in one["args"]:
                pick = "必填" if a.get("required") else "可选"
                lines.append(f"  {a['dest']:<16}{pick:<5}{a.get('help', '')}")
            lines.append("")
        if one["options"]:
            lines.append("选项：")
            for o in one["options"]:
                bits = [o["flag"]]
                if o.get("type"):
                    bits.append(f"<{o['type']}>")
                elif o.get("kind") == "value":
                    bits.append("<值>")
                meta = []
                if o.get("kind") == "append":
                    meta.append("可重复")
                if o.get("choices"):
                    meta.append("可选 " + "|".join(map(str, o["choices"])))
                if "default" in o:
                    meta.append(f"默认 {o['default']}")
                flag = "  " + " ".join(bits)
                desc = o.get("help", "") or ""
                tail = ("  （" + "；".join(meta) + "）") if meta else ""
                lines.append(flag.ljust(26) + desc + tail)
            lines.append("")
        if one["examples"]:
            lines.append("示例：")
            lines += [f"  {e}" for e in one["examples"]]
            lines.append("")
        if one.get("notes"):
            lines.append("注意：")
            lines += [f"  - {n}" for n in one["notes"]]
        return "\n".join(lines)

    out = [f"{spec['tool']} {spec['version']} — {spec['purpose']}", ""]
    out.append("用法：")
    out.append(f"  {spec['invocation']['packaged']}")
    out.append(f"  {spec['invocation']['source']}")
    out.append("")
    out.append("全局选项：")
    for o in spec["global_options"]:
        extra = f"  （默认 {o['default']}）" if "default" in o else ""
        out.append(f"  {o['flag']:<22}{o.get('help', '')}{extra}")
    out.append("")
    out.append("命令（agent 用 `hy-mem help --json` 拿完整规格）：")
    cur = None
    for c in spec["commands"]:
        if c["group"] != cur:
            cur = c["group"]
            out.append(f"  [{cur}]")
        out.append(f"    {c['name']:<10}{c['summary']}")
    out.append("")
    out.append("要点：")
    out += [f"  - {n}" for n in spec["invocation"]["notes"]]
    out.append("")
    out.append("退出码：" + "；".join(f"{k}={v}" for k, v in spec["exit_codes"].items()))
    return "\n".join(out)
