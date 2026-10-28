# AGENTS.md · hy-mem 项目交接说明

> **读者**：没有历史上下文的 AI（或接手的人）。
> **目标**：只读这一份，就能改代码、跑测试、打 exe、排障、判断该不该动。
> **边界**：本文件是**开发者视角**。面向调用方（智能体实际怎么用命令）的说明在 `skill/hy-mem/SKILL.md`，那份会被打包进产物发出去，两者不重复。
> **最后校准**：2026-10-28，对应 hy-mem **0.9.8**。文中所有数字都是实测值，附了复核命令。

---

## 0. 一句话定位

**hy-mem = 给 AI 智能体用的本地长期记忆 CLI**，外层是一套零依赖终端 UI（方向键菜单 / 设置向导 / 按键解码），底层是 Hy-Memory SDK 2.0.5 + chromadb 向量库，可 PyInstaller 打成自包含目录 `dist/hy-mem/`（355 MB，整个拷走即可用，对方不需要 Python）。

核心能力：写入记忆（同步/异步）→ LLM 抽取成 `l2_fact` / `l3_schema` → 语义检索 → 标签归档；多智能体可用 `user_id` 共享或隔离记忆。

---

## 1. 事实快照

| 项 | 值 | 复核方式 |
|---|---|---|
| 版本 | `0.9.11`（`cli/helptext.py: VERSION`） | `dist/hy-mem/hy-mem.exe --version` |
| 语言/运行时 | Python **3.12.10**，venv 在 `.venv/` | `.venv/Scripts/python.exe -V` |
| 核心 SDK | `hy-memory 2.0.5` | `.venv/Scripts/python.exe -m pip show hy-memory` |
| 向量库 | `chromadb 1.5.9` | 同上 |
| 打包器 | `PyInstaller 6.22.3` | 同上 |
| 本地嵌入（备选） | `fastembed 0.9.1` + `onnxruntime 1.31.1` | 同上 |
| 源码规模 | **33 个 py，15436 行**（`cli/` 20 个 12033 行 + `scripts/` 13 个 3403 行；`.local-test/` 里的临时脚本不计、不入库） | `cat cli/*.py scripts/*.py \| wc -l` |
| 子命令 | **31** 个（0.9.6 新增 `mcp` / `version` / `migrate`；0.9.7 新增 `update` / `history`；0.9.8 `worker` 加 `--lanes` 与延迟队列 `jobs.not_before`；0.9.9 `worker` 内嵌 chroma server；0.9.10 拆出 `serve`（数据所有者），`worker` 退回纯消费者；**0.9.11 新增 `log`**） | `grep -c "add_parser(" cli/hy_mem_cli.py` |
| 测试用例 | **421**（CLI 84 / UI 94 / setup 103 / vdb 36 / MCP 22 / queue 18 / oplog 25 / i18n 29 / encoding 10） | 见 §8 |
| 产物 | 目录 `dist/hy-mem/` **476 MB**（exe 本体 34 MB）；对外分发 `dist/hy-mem-0.9.11-win64.zip` **178 MB** | `du -sh dist/hy-mem dist/*.zip` |
| 启动开销 | `--version` / `help` 冷启 **0.4 s 量级**（实测 0.36~0.46 s，视磁盘缓存；判断「要不要换编译器」的基线） | `time dist/hy-mem/hy-mem.exe --version` |
| 构建耗时 | 约 **196 s**（含压缩包 24 s） | 构建日志末行 |
| 默认数据目录 | `<安装根>/data`（源码=`仓库/data`，exe=`dist/hy-mem/data`） | `hy-mem config show` |
| 向量维度 | **1024**（`qwen3.7-text-embedding-flash`） | `hy-mem doctor` 的 `embedding_live` |
| LLM | `deepseek-v4-flash` @ `https://newapi.jxdata.com:18000/v1`（推理模型） | `hy-mem config show` |
| 检索阈值 | `minScore = 0.35`（SDK 默认 0.4 会漏召回，已下调） | `config.json → search.minScore` |
| 版本控制 | git 仓库，远端 `gitee.com/one-person_7/hy-mem`（master）；**纯源码仓**，产物/环境/缓存不入库 | `git remote -v && git count-objects -vH` |

---

## 2. 目录地图

```
D:\tools_open\agent-bridge\
├── AGENTS.md                  ← 本文件（开发者交接说明）
├── config.json                ← 源码环境的设置（**不含密钥**，只记 keyEnv 变量名）。存在时强覆盖 .env
├── .env                       ← 源码环境的杂项/密钥兜底（不入库，别提交）
├── .env.example               ← 模板，说明每个变量的含义
├── hy-mem.cmd                 ← 源码模式启动器（cmd 壳，调 .venv 的 python）
├── hy-mem.spec                ← PyInstaller spec（注意：build_exe.py 不走它，走命令行参数）
│
├── cli\                       ← 全部业务代码（全部可改，改完必须跑测试）
│   ├── _pyi_entry.py          ← 打包入口。只做两件事：冻结进程强制 UTF-8、转发 main
│   ├── hy_mem_cli.py   (3481) ← 大脑：31 个子命令 + 全局选项搬运 + 配置闸门 + 错误分类 + stdin 解码链
│   ├── helptext.py      (672) ← 命令规格单一真源（`help` / `help --json` 的数据源）
│   ├── config.py        (499) ← config.json 读写 + env 映射 + 校验 + 身份默认值（`MEMORY_DATA_DIR` 优先于配置文件）
│   ├── netprobe.py       (276) ← 出站请求唯一出口：预检字符集 + Sent/NotSent，杜绝 HTTP 0；get_json(direct=True) 供回环探活
│   ├── vdb_endpoint.py  (484) ← chroma server 端点层（0.9.10）：`serve` 独占数据 + owner 化的 server.port；客户端 ensure_endpoint 注入/复验 MEMORY_VECTOR_HOST/PORT，端点该在却连不上就抛 EndpointUnavailable（不静默降级）；0.9.11 起用进程创建时间识破 pid 复用
│   ├── setup_wizard.py  (654) ← 四步向导（向量→模式→LLM→存储），实测维度、真调 LLM
│   ├── menu.py          (907) ← 交互主界面 + 设置中心 + 13 个页面（子命令一律子进程派发；写记忆支持连续写入）
│   ├── i18n.py          (434) ← 中英切换：中文原文当 key，`tr()` 缺译回落中文，语言来源 HY_MEM_LANG > config.lang > zh
│   ├── oplog.py         (243) ← 操作日志：`<dataDir>/ops.log`，只留最近 50 条，正文/查询词不落盘，密钥打码
│   ├── tui.py          (1367) ← 零依赖终端 UI：按键解码 / 行编辑 / 重绘 / 鼠标 / 宽度裁剪 / 折行（wrap）
│   ├── runtime_check.py (323) ← 运行环境自检：系统/位数/包完整性/可写/运行库/磁盘
│   ├── store.py         (511) ← SQLite 异步队列 + 标签索引 + 跨进程写锁
│   ├── storage_ops.py   (197) ← 数据目录运维：用量 / 剩余空间 / 迁移 / 备份 / 重建
│   ├── install_ops.py   (417) ← 生成 PATH 启动器（`hy-mem install`，双入口 + 旧壳接管 + 升级时沿用旧安装配置/数据）
│   ├── skill_ops.py     (106) ← SKILL.md 定位 / 一致性 / 分发到 5 个宿主
│   ├── mcp_server.py    (739) ← MCP stdio 服务（`hy-mem mcp`）：JSON-RPC 2.0，8 个工具（6 记忆 + queue_status + get_version），进程内直调 SDK，日志走 stderr
│   ├── presets.py       (239) ← 向量/LLM/模式预设（数字全部实测，别凭想象改 dims）
│   └── local_embedder.py (112) ← 本地 ONNX 嵌入（零 key 备选，512 维，fastembed）
│
├── scripts\                   ← **只放业务相关**：构建 / 提交闸门 / 密钥入口 / 产品测试套件（不进产物）
│   ├── build_exe.py     (507) ← 打包主脚本：消毒 → SKILL.md → 启动器/说明 → 压缩包 → 清历史
│   ├── check_secrets.py (260) ← 提交前密钥/隐私闸门（ERROR 挡提交，WARN 要人看一眼）
│   ├── _secrets.py      (121) ← 密钥唯一入口：只读 `MEMORY_EMBEDDER_API_KEY` / `MEMORY_LLM_API_KEY`
│   ├── test_cli.py      (367) ← 接口契约（退出码 / 输出分流 / 全局选项位置 / 身份）
│   ├── test_ui.py       (417) ← 按键与渲染 + 连续写入的循环语义（假终端，无需真 TTY）
│   ├── test_setup.py    (471) ← 向导 / 存储运维 / 队列积压（不发网络请求）
│   ├── test_vdb_endpoint.py (282) ← 端点层：归属校验 / 六个 ensure 分支 / pid 复用 / 真实 chroma 线程
│   ├── test_encoding.py (118) ← stdin 解码链 + 代理项闸门 + **复刻事故环境**（PYTHONIOENCODING=gbk:surrogateescape）
│   ├── test_oplog.py    (151) ← 操作日志：只留 50 条 / 脱敏 / 写不进去也不影响命令
│   ├── test_i18n.py     (144) ← 中英切换：默认 zh 逐字不变 / en 生效 / 缺译回落
│   ├── test_queue.py    (163) ← 队列核心（旧库迁移 / claim 原子性 / 退避 / 死信）
│   ├── test_mcp.py      (252) ← MCP stdio 八工具端到端
│   └── test_concurrency.py (150) ← 并发写 + 检索风暴（独立沙箱）
│
├── .local-test\               ← 临时/诊断脚本，**整目录 `.gitignore` 忽略、永不入库**
│                                 probe_*（巡检/探针）、graph_ab_*（检索 A/B 实验）、
│                                 bench_embed（嵌入跑分，presets 数字出处）、
│                                 bug 复现、一次性迁移、历史遗留 doctor.py
│
├── skill\hy-mem\SKILL.md      ← 面向调用方的用法说明（516 行）。打包时复制进产物
├── data\                      ← 源码环境的记忆库 + 队列库（**本地运行时数据，不入库**；目录由程序自建）
├── requirements.txt           ← 环境重建清单（114 个包，pip install -r 一键装回）
├── .models\                   ← fastembed 的 ONNX 模型缓存（本地生成，不入库；首跑自动下载）
├── .venv\                     ← 虚拟环境（本地生成，不入库；pip install -r requirements.txt 重建）
├── build\                     ← PyInstaller 中间物（本地生成，不入库）
└── dist\                      ← 产物（本地生成，不入库；`python scripts/build_exe.py` 约 196s）
    ├── hy-mem\                ← 产物目录：_internal/ + hy-mem.exe + skill/ + 启动器 (+ 自用 config/data)
    └── hy-mem-0.9.11-win64.zip ← 对外分发物（178MB，解压即完整目录）
```

**别在这几处凭直觉改**：`helptext.py` 是帮助文本唯一真源（改命令必须同步改它）；`presets.py` 的 `dims` 必须与真实模型一致；`SKILL.md` 只改 `skill/hy-mem/SKILL.md`，宿主里的副本用 `hy-mem skill --install` 铺过去。

**两个脚本目录别放错**（2026-10-28 约定的硬规则）：

| 目录 | 放什么 | 进 git？ |
| --- | --- | --- |
| `scripts/` | **业务相关**：构建、提交闸门、密钥入口、产品测试套件 | ✅ 进 |
| `.local-test/` | **临时/诊断**：探针巡检、A/B 实验、bug 复现、一次性迁移、历史遗留 | ❌ 整目录被 `.gitignore` 忽略 |

- 排查 bug 时新写的脚本一律落 `.local-test/`，不要往 `scripts/` 里塞——仓库要公开，这类脚本最容易带上本机路径、内部主机名、测试数据。
- `.local-test/` 脚本要 key 就加一行路径后 `import _secrets`（只读环境变量），**不许明文**；也不许指向真实库，跑前自己设 `HY_MEM_CONFIG` + `MEMORY_DATA_DIR` 沙箱。
- 某个探针一旦成为发版必跑的门禁，就**转正**成 `scripts/test_*.py` 的用例并计入套件总数，别继续躺在 `.local-test/`。详见 `.local-test/README.md`。

---

## 3. 启动链路（改 `main()` 前必读）

`cli/hy_mem_cli.py: main()` 的执行顺序不是随手排的，每一步都对应一次事故：

| # | 步骤 | 为什么必须在这个位置 |
|---|---|---|
| 1 | `_force_utf8()` | 冻结进程读不到 `PYTHONIOENCODING`，不先改编码，中文输出变 GBK 乱码 |
| 2 | `load_env()` | 先装 `.env`，再由 `config.json` **覆盖**它；`store.py` 依赖 `MEMORY_DATA_DIR`，晚一步队列库就落到 `~/.hy_memory/` 与主库分家 |
| 3 | `hoist_globals(argv)` | 把 `--json/--config/-u` 等搬到最前，让全局选项**位置无关**（智能体习惯追加在末尾）。`--` 之后按字面量处理 |
| 4 | `_Parser.parse_args` | 自定义 `_Parser.error` 把用法错误从 argparse 默认的 `2` 改成 `1`，避免与「运行时错误」撞号 |
| 5 | 记录 `--quiet/--json/--fields` | `--quiet` 直接把 `sys.stdout` 换成 StringIO（比逐处改 print 干净）；`--json` 例外，那份输出本身就是结果 |
| 6 | 身份回填 `args.user` | 未传 `-u` 时填 `config.identity.userId` → `HY_MEM_USER_ID` → `default`。**这一步修的是真实报错**：`search` 用复数形参，`[None]` 会被 SDK 抛 `user_ids is required and cannot be empty` |
| 7 | `--help-json` 短路 → `cmd_help` | 智能体最省事的一条路，不必进子命令 |
| 8 | `logging.disable(ERROR)` | SDK 的 logger 自带 handler 且 `propagate=False`，只能全局压；不压的话 reconcile 会把 `BM25_ALL_ZERO` 这类正常统计打成 ERROR 混进 stdout，agent 解析失败。交互模式（menu/setup）和 `--verbose` 除外 |
| 9 | 无子命令分流 | TTY → 首次设置向导 + 交互主界面；**非 TTY（被 agent / 管道调用）→ 只打简短帮助并 exit 0**，绝不阻塞等输入 |
| 10 | `args.func(args)` + 异常兜底 | `KeyboardInterrupt`→130；`Aborted`（UI 取消）→0；其余→`classify_error()` 映射 2/3，`--json` 下额外在 stdout 给结构化错误 |

### 3.1 错误与退出码

| 退出码 | 含义 | agent 该怎么做 |
|---|---|---|
| 0 | 成功（含「已取消」） | 继续 |
| 1 | 用法错误 / 缺输入 / 破坏性操作未加 `--yes` / **配置不全被闸门拦下** | 按提示改参数或补配置 |
| 2 | 运行时错误（端点不可达、密钥失效、维度不匹配） | 可修复可重试，别当程序缺陷 |
| 3 | 内部错误（TypeError/KeyError 等） | 加 `--verbose` 拿堆栈，别反复重试 |
| 130 | Ctrl+C 中断 | 无副作用，可重跑 |

`--json` 时失败在 **stdout** 也有一份：`{"ok":false,"error":{"code":"E_USAGE|E_RUNTIME|E_INTERNAL|E_CONFIG","message":"...","hint":"...","missing":[...]}}`。

顶层冒出的 `UnicodeEncodeError` 被归为 **`E_CONFIG` / 退出码 1**（不是 2）：它意味着「HTTP 头里有非 ASCII 字符」，改配置就能解决，不是网络故障。这条映射在 `classify_error()` 里，兜住所有绕过闸门的路径（例如 SDK 内部直接用 `httpx` 发请求）。

---

### 3.2 向量库端点架构（0.9.10，改 `make_client` / `cmd_serve` / `cmd_worker` 前必读）

chroma embedded 是单进程嵌入式库，**不支持跨进程共享**：worker 写入并翻转 supersede 后，其他进程的内存索引不更新，召回池直接为空（2026-10-28 实测事故，且不自愈）。因此数据必须由一个进程独占。

**0.9.10 的关键修正：把「数据所有者」和「队列消费者」拆成两个命令。** 0.9.9 让 `worker` 兼当宿主，消费者跑完就退、顺手把 server 带走，还无条件删掉别人的端口登记——于是「检索→写入→立即消费→再检索」直接报 `Could not connect to a Chroma server`，此后整个会话全废（PRD F1/F2/F5）。

| 角色 | 命令 | 生命周期 | 硬约束 |
|---|---|---|---|
| 数据所有者 | `hy-mem serve` | 长驻单例，空闲 `--idle-exit`（默认 1800s）自退 | 同一数据目录只能有一个；抢输的那个立即收摊 |
| 队列消费者 | `hy-mem worker` | 短命（`--once` / 空闲 30s 退） | **永不起 server**，自己是 HttpClient 客户端 |

`ensure_endpoint()` 的分支顺序每条都对应一个实测缺陷：

1. `MEMORY_VECTOR_HOST` 有值且**无** `MEMORY_VECTOR_HOST_SRC=hymem` → 外部配置（远程 chroma/qdrant），不动；
2. 有值且带标记（自己注入的）→ **每次复验心跳**，失效就清掉重取（0.9.9 把它当外部配置，进程内坏一次永久坏）；
3. 端口文件主人 pid 活着且心跳可达 → 采纳；
4. 主人活着但连不上 → 等一轮后抛 `EndpointUnavailable`（`E_ENDPOINT`，exit 2），**绝不静默回落嵌入式**——那会读到别人写过的旧索引；
5. 主人已死（`store.pid_alive`：Windows `OpenProcess` / POSIX `kill-0`）→ 清残留登记 → 拉起 `serve` 再等；
6. 拉起失败且从无主人 → 才允许回落嵌入式，并在 stderr 明确告警。

其他要点：

- 端口文件 `data/server.port` 带 `{port, pid, owner, started_at}`，写入走临时文件 + `replace`（读方不会看到半截 JSON）；删除必须给 `expect_pid`——**只删自己的登记**（0.9.9 的 F2 就是无差别删除）。
- 交互界面（`menu.py`）的子命令一律**子进程**派发（`menu.subprocess_dispatch`）：同进程跑 worker 会把后台角色写进本进程 env。
- 拉起闸门 `claim_worker_slot` 会先清「pid 已不存在」的注册行（`store._purge_dead_pids`，只对 3s 以上的行探活），否则被 SIGKILL 的 worker 要占满 15s 名额，期间检索只能干等（F10）。
- serve 主体是 `vdb_endpoint.start_server_thread`（uvicorn 线程 + `_patch_api_embeddings`），外加 ASGI 包装记录最后请求时间供空闲退出。
- 注入时同步补 `NO_PROXY=127.0.0.1,localhost`——chroma 的 httpx 吃代理环境变量与 Windows 系统代理，回环被代理劫持即 502（实测）。
- 两个实测坑：chroma `Settings` 必须传 `is_persistent=True`（否则 server 静默变内存库，检索永远空）；`chroma_api_impl` 不可传 server 类（System 递归构造自爆）。
- 第三个坑（同日二次事故，`tolist`）：跨进程写入落在向量段 brute force/queue 路径时 `GetResult.embeddings` 是 list，而 FastAPI 响应层硬编码 `cast(Embedding, e).tolist()` → **HTTP 的 `get`/`query` 一带 embeddings 就 500**；SDK 的 `get_by_ids`/`get_embeddings` 静默返回空 → `expand_evolution_chains` 整批走 fallback（`READ_EVOLUTION evolved_count=0`）→ superseded 条目漏进结果（「最美/最丑/最帅」三条同时返回，正是 0.9.9 首包的症状）。修法：`_patch_api_embeddings()` 在 API 实例的 `_get`/`_query` 出口把 list 归一化成 numpy，实例级包装，不依赖 `chroma_api_impl` 解析出的具体类（1.5.9 默认是 `RustBindingsAPI`，不是 `SegmentAPI`）。
- 排查口诀：**检索多出历史版本 → 先验 HTTP 读 embeddings**（`.local-test/probe_vdb_http.py`），失败就是上面那个坑，别一头钻进检索管线。
- 分发前的两条实测：`.local-test/probe_vdb_http.py`（已装 server 的 embeddings 契约）、`.local-test/probe_mcp_stdio.py`（真实 stdio：initialize → tools/list → search/add）。两者默认指向 `dist/hy-mem/hy-mem.exe`，可用 `HYMEM_EXE` 覆盖。（这两个探针在 `.local-test/`，本机有、仓库里没有。）
- 新增依赖 `fastapi` + `opentelemetry-instrumentation-fastapi`（chromadb 未把前者列为运行时依赖），打包需 `--collect-submodules fastapi uvicorn opentelemetry.instrumentation.fastapi`（uvicorn/otel 有 importlib 动态加载）。

### 3.3 终端显示口径：折行，不截断（0.9.10，改任何结果输出前必读）

**记忆正文一行放不下就换行，禁止把正文截断成 `xxx…`。** 起因：检索结果里演化链的 `content` 本身就是多行（`- Current: …` / `When:` / `History:` / 各旧版本），旧实现先 `.replace("\n"," ")` 压平再 `[:150]`/`trunc(90)`，用户只看到 `用户认…`，误以为记忆内容就这么短（2026-10-28 反馈）。

| 层 | 做法 |
|---|---|
| `tui.wrap(text, width)` | 按东亚字宽折行；保留原有换行；拉丁词整体不拆（时间戳/URL 不劈开）；标点悬挂；续行沿用缩进 |
| `tui.table()` | 单元格支持 `\n` 多行，同行各列顶部对齐（调用方自己算列宽再 wrap 传进来） |
| 交互页（`menu.py`） | 内容/说明列宽 = `term_width()` − 固定列 − 分隔符，`max(24, …)` 兜底；检索页底部给 `看全文：hy-mem get <id>` |
| 非交互（`cmd_search` / `cmd_list` / `cmd_history`） | 多行正文按原始缩进对齐展开（`pad = len(head)`），不再压平、不再按字符截断 |

判定标准：管道或窄终端下逐行宽度 ≤ `term_width()`，且**任何一条记忆的 History 都能完整看到**（`scripts/test_ui.py` 的宽度裁剪用例守着这条）。

### 3.4 stdin 编码：一律按字节读（0.9.11，改任何「从 stdin 收正文」的路径前必读）

**禁止用 `sys.stdin.read()` 收记忆正文。** 冻结进程（PyInstaller）的 `sys.stdin` 是**系统 locale 编码 + surrogateescape**（中文 Windows = cp936），而上游按 UTF-8 喂字节：cp936 会把字节切错，剩下的零头变成孤立代理项 `\udcXX`，一路活到「建 HTTP 请求体」才炸：

```
UnicodeEncodeError: 'utf-8' codec can't encode character '\udcaa' in position 17: surrogates not allowed
```

现场还把这句误报成「密钥或地址含非 ASCII」，照着改密钥永远修不好（2026-10-28 线上事故）。

| 层 | 做法 |
|---|---|
| 读 | `hy_mem_cli._read_text` 读 `sys.stdin.buffer`，用 `_decode_stdin` 显式解：BOM → UTF-8 → GBK → `replace` 兜底；GBK 命中时 stderr 告警（猜错会写乱码，不能静默） |
| 闸 | `_strip_surrogates` 在拼请求体之前剔除孤立代理项并告警，保证「无论如何都能 utf-8 编码」 |
| 报 | `errors.classify_error` 按消息分流：含 `surrogate` → `E_ENCODING` 指向输入侧；`latin-1`/position N → 才是密钥/地址 |
| 验 | `scripts/test_encoding.py`（含**复刻事故环境**的子进程用例：`PYTHONIOENCODING=gbk:surrogateescape` 下 `_read_text` 必须逐字正确） |

反面教材：不要改成「把 stdin reconfigure 成 utf-8」——那样 `cmd.exe` 的 GBK 管道会变成一堆替换字符，静默乱码比报错更糟。

### 3.5 操作日志与界面语言（0.9.11）

| 机制 | 口径 |
|---|---|
| 操作日志 | `cli/oplog.py`：`<dataDir>/ops.log`，**只留最近 50 条**（超限截尾 + 原子替换）；在 `main()` 统一计时记退出码，CLI 与 TUI（子进程调 CLI）一次覆盖；`add/update/search` 的**正文与查询词不落盘**，密钥类参数打码 `***`；写失败静默（`--verbose` 才吭声），绝不影响命令。查看 `hy-mem log` 或设置页「操作日志」 |
| 界面语言 | `cli/i18n.py`：中文原文当 key，`tr()` 缺译**回落中文**；优先级 `HY_MEM_LANG` > `config.lang` > `zh`；`hy-mem config set lang=en` 立即生效。覆盖交互界面全部页面 + 错误提示 + 常用命令摘要；命令行正文输出仍为中文（测试断言了大量中文字面量，默认 zh 必须逐字不变） |

新增界面文案时：`menu.py` 用 `tr("中文")` 包一层并在 `i18n.EN` 补一行；忘记补只是回落中文，不会报错。

## 4. ⚠️ 两套完全独立的环境（最高频误解）

源码与 exe **不共享** config / data / logs。这是有意设计（产物要能整个目录拷给别人），不是 bug。

| | 源码（开发） | exe（分发） |
|---|---|---|
| 根目录 | `D:\tools_open\agent-bridge\` | exe 所在目录 `…\hy-mem\` |
| 判定 | `sys.frozen` 为假 | `sys.frozen` 为真 |
| 配置 | `仓库/config.json` | `dist/hy-mem/config.json` |
| 数据 | `仓库/data/` | `dist/hy-mem/data/` |
| 当前身份 | `huchao`（隔离） | `default`（通用） |
| 记忆条数 | 20 条 | 独立一套 |

**症状 → 结论**：「我明明配好了，却报配置不全」= 命令走的不是你以为的那一套。
**第一步永远是**：`hy-mem config show` 看「配置文件」那行的路径属于哪一边。

覆盖方式（排查用，不要当常规操作）：`HY_MEM_HOME=<根>` 或 `HY_MEM_CONFIG=<文件>`。

---

## 5. 配置体系

### 5.1 优先级

```
命令行参数  >  config.json  >  .env  >  进程真实环境变量
密钥相反：环境变量  >  config.json（配置文件里根本不存明文）
```

`config.json` 一旦存在就是**唯一真源**，会覆盖 `.env` 同名项（因为它是用户在向导里的显式选择）。改了 `.env` 不生效，先看 `config show`。

**密钥是唯一例外**，方向也相反：`embedding.keyEnv` / `llm.keyEnv` 只记**变量名**，真实值从环境变量取（`MEMORY_EMBEDDER_API_KEY` / `MEMORY_LLM_API_KEY`）。这样仓库可以公开、换 key 只动环境变量、`config.json` 备份随手贴出去也不泄漏。解析入口是 `config.resolve_embed_key()` / `config.resolve_llm_key()`，`doctor` 会显示「来自 MEMORY_EMBEDDER_API_KEY」还是「来自 config.json」。

### 5.2 config.json 关键字段

| 字段 | 说明 |
|---|---|
| `mode` | `lite` 纯向量（快，无 LLM）/ `pro` 加 LLM 抽取（默认）/ `ultra` 再加 Schema 归纳 |
| `embedding.{provider,baseUrl,model,apiKey,keyEnv,dims}` | baseUrl/model/dims 必须**原子一起改**，否则 `EmbeddingDimensionMismatchError`；`apiKey` 留空，从 `keyEnv`（默认 `MEMORY_EMBEDDER_API_KEY`）取 |
| `llm.{baseUrl,model,apiKey,keyEnv,insecureTls}` | `apiKey` 留空，从 `keyEnv`（默认 `MEMORY_LLM_API_KEY`）取，避免密钥副本 |
| `storage.dataDir` | 留空 = 安装根下的 `data`（**只写回非默认值**，防止把机器绝对路径固化） |
| `identity.userId` | 空 = 通用（`default`，多智能体共享）；有值 = 隔离 |
| `search.minScore` | 0.35 |
| `queue.{maxAttempts,lockTimeout,staleSec}` | 3 / 180 / 900 |
| `health.*` / `ui.*` | 健康阈值、颜色、语言 |

### 5.3 高频环境变量

| 变量 | 作用 |
|---|---|
| `MEMORY_MODE` / `MEMORY_DATA_DIR` | 模式 / 数据目录 |
| `MEMORY_EMBEDDER_{PROVIDER,BASE_URL,MODEL,API_KEY}` / `MEMORY_EMBEDDING_DIMS` | 嵌入后端（`openai` 云端 / `local` ONNX） |
| `MEMORY_LLM_{BASE_URL,MODEL,API_KEY}` / `YSJX_API_KEY` | LLM（key 由后者继承） |
| `HY_MEM_USER_ID` | 默认身份，优先级高于 config |
| `HY_MEM_CONFIG` / `HY_MEM_HOME` / `HY_MEM_DATA_DIR` | 覆盖配置路径 / 安装根 / 数据目录 |
| `HY_MEM_MIN_SCORE` / `HY_MEM_ASYNC` / `HY_MEM_NO_PROBE` | 阈值 / add 默认异步 / 空结果自证探测开关 |
| `HY_MEM_TUI=0` / `HY_MEM_MOUSE=1` | 退回编号菜单 / 开鼠标上报（默认关，开着会抢走终端拖选与右键复制） |
| `HF_ENDPOINT` | 模型下载入口，**默认 `https://hf-mirror.com`**（国内镜像；fastembed 默认拉 huggingface.co 会卡 40s 再降级） |
| `MEMORY_LOCAL_EMBED_CACHE` | 本地 ONNX 模型缓存目录，默认跟 `MEMORY_DATA_DIR/models` 走（换包不重下 92MB） |
| `HY_MEM_DIST` | **构建期**：产物输出目录 |
| `HY_MEM_TEST_EXE` | **测试期**：让契约测试打 exe 而不是源码 |

### 5.4 配置闸门（`readiness()` / `require_ready()`）

`add / search / list / get / delete / forget / worker / tags --refresh / stats --health` 执行**前**会检查配置完整度。缺项时不抛 SDK 的 401 堆栈，而是逐条列出「缺什么 / 怎么补（可直接复制的命令）/ 为什么拦」，退出码 1，`--json` 给 `E_CONFIG` + `missing[{item,what,fix}]`。

**动机**：配不全的旧表现是「httpx 401 堆栈」或「静默返回空列表」，两者都会被误读成「程序坏了 / 没有记忆」。

### 5.5 user_id 语义

| 值 | 含义 |
|---|---|
| 不设（`default`） | **通用**：所有智能体共享同一份记忆 —— 默认行为 |
| 任意名字 | **隔离**：自成一套记忆，与其它 id 互不可见 |

切换：`hy-mem user set <id>` / `hy-mem user reset` / `hy-mem user list`。**只改 `identity.userId`**，不动模型、数据目录、已有记忆（切走只是看不到，切回来还在）。
临时只影响一条命令：`hy-mem -u writer search "..."`。

---

## 6. 数据与状态

```
<dataDir>/
├── data/                 ← SDK 主库
│   ├── vector_db/        ← chromadb 向量（维度与 MEMORY_EMBEDDING_DIMS 强绑定）
│   ├── cache.db
│   └── history.db
├── hy_mem_queue.db       ← 异步队列 + 标签索引（SQLite，单文件）
├── logs/hy_memory.log    ← 运行日志（logs/pipeline/ 为管道追踪）
└── write.lock            ← 跨进程写锁（OS 字节锁，进程崩溃自动释放）
```

| 关注点 | 事实 |
|---|---|
| 备份 | **拷整个 dataDir**（含 `hy_mem_queue.db`）。`write.lock` 不用备份 |
| 迁移 | `storage_ops.migrate()`：拷完**不删源**（长期记忆，删源不可逆） |
| 换嵌入模型 | 维度或语义空间变了 → **必须重建 data 目录**，否则旧向量读不出来 |
| 队列积压 | `hy-mem queue` 看 `pending/running/dead`；`worker --once` 消费；`retry dead` 重投 |
| 写锁 | SDK **内部完全没有文件锁**，reconcile 是「读旧→比较→覆盖写」，多进程并发必丢更新。所有写操作都必须包在 `store.WriteLock()` 里 |

---

## 7. 命令面（29 个）

| 分组 | 命令 |
|---|---|
| 元信息 / 交互 | `help` `menu` `setup` `config` `user` `doctor` `install` `skill` `tui-test` |
| 写入 / 读取 | `add` `search` `list` `get` `delete` `update` `forget` `finalize` `history` |
| 异步队列 | `worker` `queue` `drain` `retry` |
| 标签 / 统计 | `tags` `untag` `archive` `stats` |

**全局选项**（位置无关，`hoist_globals` 负责）：`--json` `--quiet` `--no-color` `--verbose` `--help-json` `--fields` `--config` `--mode` `--data-dir` `-u/--user` `-a/--agent` `-s/--session` `--version`。
命令级：`delete --dry-run` / `forget --dry-run --yes`。

**规格的单一真源**是 `helptext.py: CMD_META` —— 新增/修改命令必须同步它，否则 `help` 与 `help --json` 会说错。`--fields=a,b` 只保留指定顶层字段（省 agent 的 token）。

---

## 8. 测试矩阵

| 套件 | 用例 | 覆盖 | 跑法 |
|---|---|---|---|
| `scripts/test_ui.py` | **94** | 按键解码（Windows 扫描码 / VT / SGR 鼠标 / DSR）、行编辑、热键、宽度裁剪、不折行、菜单导航、**写记忆连续写入的循环语义** | `.venv/Scripts/python.exe scripts/test_ui.py` |
| `scripts/test_setup.py` | **103** | 向导各步、存储运维、推理模型探活判定、**密钥字符集（中文粘贴）**、**出站请求收口（源码守卫）**、队列积压可见、**运行环境自检 + 双击启动器 + HF 镜像** | `.venv/Scripts/python.exe scripts/test_setup.py` |
| `scripts/test_cli.py` | **84** | 退出码、stdout/stderr 分流、全局选项位置无关、`--fields`、身份、`E_CONFIG`（缺项 / **非 ASCII 密钥**） | 见下 |
| `scripts/test_queue.py` | **18** | 0.9.8 队列核心：旧库 not_before 幂等迁移、claim 原子性、退避曲线（2/4s）、未到期不可领、死信与 retry 重投、requeue_stale 清延迟、spawn 闸门 cap=1 | `.venv/Scripts/python.exe scripts/test_queue.py` |
| `scripts/test_concurrency.py` | 场景 | 4 并发 add --async + 2 MCP 检索进程风暴（独立沙箱，等队列 idle 判定） | `.venv/Scripts/python.exe scripts/test_concurrency.py [exe]` |
| `scripts/test_mcp.py` | **22** | MCP stdio 八工具端到端：握手/tools/list/add→**queue_status 进度**→落库→search/list/get/update/delete/get_version、confirm 守门、stderr 无栈 | `.venv/Scripts/python.exe scripts/test_mcp.py [exe]` |
| `scripts/test_vdb_endpoint.py` | **36** | 端点层契约：端口文件 owner 校验、**pid 复用识破**、心跳、六个 ensure 分支（外部/复验/主人活着失败/主人已死/拉起/自注入失效）、真实 server 线程读写与持久性 | `.venv/Scripts/python.exe scripts/test_vdb_endpoint.py` |
| `scripts/test_encoding.py` | **10** | stdin 解码链（UTF-8/GBK/BOM/UTF-16/垃圾）、代理项闸门、**复刻事故环境**（`PYTHONIOENCODING=gbk:surrogateescape`） | `.venv/Scripts/python.exe scripts/test_encoding.py` |
| `scripts/test_oplog.py` | **25** | 操作日志：只留 50 条且保留最新、脱敏、正文不落盘、写不进去也不影响命令 | `.venv/Scripts/python.exe scripts/test_oplog.py` |
| `scripts/test_i18n.py` | **29** | 中英切换：默认 zh 逐字不变、`HY_MEM_LANG=en` 生效、缺译回落中文、`config set lang=en` 读到 en | `.venv/Scripts/python.exe scripts/test_i18n.py` |

```bash
# 源码模式
.venv/Scripts/python.exe scripts/test_cli.py

# 打 exe 模式（打包后必须再跑一遍，同一个脚本）
HY_MEM_TEST_EXE=dist/hy-mem/hy-mem.exe .venv/Scripts/python.exe scripts/test_cli.py
```

三套都**不需要真终端**（test_ui 用假 Reader）、**不需要网络**（test_cli 只跑 help/config/queue/install/参数错误）。

**验收口径**：九套全绿（cli 84 / ui 94 / setup 103 / vdb 36 / mcp 22 / queue 18 / oplog 25 / i18n 29 / encoding 10 = **421**）+ exe 上再跑一遍 test_cli 全绿 + `doctor` 全 PASS（19 项），三者同时满足才算完成。

---

## 9. 构建前置清单（Checklist）

打 exe 前逐条过，任何一条不满足都会导致「构建中途失败」或「产物不可用」：

| # | 检查项 | 命令 | 不满足的后果 |
|---|---|---|---|
| 1 | 没有 hy-mem 进程占用产物目录 | `tasklist \| grep -i hy-mem` | Windows 目录被占用 → `_stamp_old()` 改名失败 → exit 1 |
| 2 | 目标目录不存在或已清空 | `ls dist-next 2>/dev/null` | PyInstaller 可能混入旧文件 |
| 3 | 三套测试全绿 | 见 §8 | 把已知 bug 打进发布包 |
| 4 | 版本号已更新（有功能改动时） | `grep VERSION cli/helptext.py` | 两个包版本相同，无法区分 |
| 5 | **已备份 exe 侧的 config.json 与 data** | `cp dist/hy-mem/config.json ~/.bak.json` | 构建消毒会删掉它们，用户配置丢失 |
| 6 | `.venv` 里有 pyinstaller | `ls .venv/Scripts/pyinstaller.exe` | 回落到 PATH 上的 pyinstaller（版本可能不同） |
| 7 | `skill/hy-mem/SKILL.md` 是最新的 | `hy-mem skill` 看 md5 | 产物带的说明过期 |

**第 5 条最容易忘**：产物目录同时是「你的自用安装目录」和「构建输出目录」。构建脚本按发布纪律会删掉里面的 `config.json` / `.env` / `data/`。

---

## 10. 构建 SOP（规范过程，照抄即可）

```bash
cd /d/tools_open/agent-bridge
```

**Step 1 · 确认无进程占用**
```bash
tasklist | grep -i hy-mem        # 有输出就先关掉那个窗口
```

**Step 2 · 备份 exe 侧配置与数据**（构建会删）
```bash
cp dist/hy-mem/config.json /c/Users/Administrator/.hy-mem-config-backup.json
cp -r dist/hy-mem/data /c/Users/Administrator/.hy-mem-data-backup
rm -f /c/Users/Administrator/.hy-mem-data-backup/write.lock
```

**Step 3 · 跑三套测试**
```bash
.venv/Scripts/python.exe scripts/test_ui.py
.venv/Scripts/python.exe scripts/test_setup.py
.venv/Scripts/python.exe scripts/test_cli.py
```

**Step 4 · 构建到旁路目录**（避免直接覆盖正在用的产物）
```bash
HY_MEM_DIST="D:/tools_open/agent-bridge/dist-next" .venv/Scripts/python.exe scripts/build_exe.py
```
正常输出末尾应含：`已确认产物不含 config.json / .env / 密钥`、`完成，用时 ~90s，体积 355 MB`。
**退出码 2 = 产物里扫到疑似密钥**（`sk-…` / `Bearer …` / `AKIA…`），必须查清是哪个文件带进来的，不能忽略。

**Step 5 · 验证产物干净、版本正确**
```bash
ls dist-next/hy-mem/                     # 应只有 _internal/ hy-mem.exe skill/
find dist-next/hy-mem -maxdepth 1 \( -name config.json -o -name .env -o -name data \) | wc -l   # 必须 0
dist-next/hy-mem/hy-mem.exe --version
ls dist-next/hy-mem-*.zip                 # 对外分发物，应该和 hy-mem/ 同级
```

**Step 5.5 · 解压冒烟（务必做，这是「发给别人能跑」的唯一证据）**
```bash
mkdir -p /d/tmp-hm/unzip && cd /d/tmp-hm/unzip && rm -rf hy-mem-* 
unzip -q "$OLDPWD/dist-next/hy-mem-0.9.5-win64.zip"   # 或资源管理器右键解压
cd hy-mem-0.9.5-win64
cmd //c "启动 hy-mem.cmd" --version                    # 应打印 hy-mem 0.9.5
```
此时目录里**没有** config.json / data/，能打印版本 = 包是自包含的。这一步在开发机上「看起来多余」，但它正是别人拿到包后的真实起点。

**Step 6 · 恢复配置与数据，跑 exe 自检**
```bash
cp /c/Users/Administrator/.hy-mem-config-backup.json dist-next/hy-mem/config.json
cp -r /c/Users/Administrator/.hy-mem-data-backup dist-next/hy-mem/data
rm -f dist-next/hy-mem/data/write.lock
dist-next/hy-mem/hy-mem.exe doctor          # 期望：环境可用 + 11 项 PASS
```
> 这一步只是为了让**你自用的那份**继续可用。真要发给别人，给的必须是 Step 5 的干净目录（无 config/data）。**先验证干净，再恢复**——顺序反了会掩盖消毒失败。

**Step 7 · 契约测试打 exe**
```bash
HY_MEM_TEST_EXE=dist-next/hy-mem/hy-mem.exe .venv/Scripts/python.exe scripts/test_cli.py
```

**Step 8 · 替换到位 + 收尾**
```bash
mv dist/hy-mem "dist/_old-hy-mem-$(date +%H%M%S)"   # 用 mv 改名，不要 rm -rf
mv dist-next/hy-mem dist/hy-mem
rmdir dist-next 2>/dev/null

cp skill/hy-mem/SKILL.md dist/hy-mem/skill/hy-mem/SKILL.md   # 产物内副本
.venv/Scripts/python.exe cli/hy_mem_cli.py skill --install   # 铺到 5 个宿主
rm -rf dist/_old-hy-mem-*                                    # 每个 355MB，别攒
```

**关于删除**：`rm -rf` 一个 1400+ 文件的目录会触发宿主的安全删除保护（中途失败）。优先 `mv` 改名，`rm -rf` 只用在小目录上；改名后的旧目录确认新版可用再删。

---

## 11. PyInstaller 参数逐条由来

参数不是照抄模板，每条都对应一次事故（写在 `build_exe.py` 顶部注释里）：

| 参数 | 不加会怎样 |
|---|---|
| `--onedir`（不用 `--onefile`） | onefile 每次启动解包 ~290MB，冷启不可接受 |
| `--collect-submodules chromadb --collect-data chromadb` | **构建全绿、doctor 全 PASS，但一写入就 `ModuleNotFoundError`** —— chromadb 大量模块按名字运行时导入，静态分析看不见。这是最隐蔽的坑 |
| `--collect-submodules hy_memory` | 同上，SDK 内部子模块加载失败 |
| `--copy-metadata hy-memory --copy-metadata chromadb` | `--version` 显示 `0.1.0`（SDK 靠 dist-info 读版本，冻结后没有） |
| `--distpath <DIST>` | 只改脚本里的 `DIST` 变量不够：PyInstaller 默认仍写 `cwd/dist`，于是指定了 `HY_MEM_DIST` 它却回头去删 `dist/hy-mem` |
| `--hidden-import store/config/tui/...` | 部分是函数内延迟 import，显式声明更稳 |
| `--exclude-module tkinter/matplotlib/IPython/pytest` | 白多带 60~100MB |
| `--paths cli` + 入口 `cli/_pyi_entry.py` | 冻结入口必须在任何输出之前 reconfigure 编码（冻结进程不认 `PYTHONIOENCODING`） |

构建后脚本自动做三件事（都用 try 包住，单步失败不影响产物可用性，但会打印）：

1. **消毒（发布纪律：包里不得内置任何模型 key）** — 递归删任意层级的 `config.json` / `config.json.tmp` / `.env*` / `data/`，再扫文本文件里的密钥特征。命中 → **返回 2 判构建失败**。带 key 的包发出去，比构建失败严重得多。
2. **带 SKILL.md** — 复制进 `dist/hy-mem/skill/hy-mem/`，别人那边 `hy-mem skill --install` 才铺得出去。
3. **清历史产物与 `build/` 缓存** — 每次 355MB，攒几次就塞满盘。

---

### 11.1 分发形态：目录 / 压缩包 / 双击启动器

**`hy-mem.exe` 只是引导器，真正的 Python 在 `_internal/` 里。** 只把 exe 拷给别人，双击必闪退。实测（只拷 exe 到空目录）：

```
[PYI-21264:ERROR] Failed to load Python DLL '...\_internal\python312.dll'.
LoadLibrary: ?????????
```

双击场景下这条消息根本来不及看——窗口立刻关闭，用户只能报「点开就闪退」。**这不是缺 Python、也不是版本不对**：运行时（`python312.dll` / `vcruntime140.dll` / 标准库 / chromadb）全在包里，只是没跟着 exe 一起被拷走。

所以构建会额外产出：

| 文件 | 作用 |
|---|---|
| `dist/hy-mem/` | 目录版，整个拷走即用 |
| `dist/hy-mem-<ver>-win64.zip` | **对外分发就用这个**（152 MB / 1417 文件），解压即完整目录，从根上排除漏文件 |
| `启动 hy-mem.cmd` | 双击入口：**在 exe 之前**检查 `_internal/`，缺了打中文说明并 `pause`，窗口不消失 |
| `安装到 PATH.cmd` | 一键 `hy-mem install` |
| `使用说明.txt` | 三步上手 + 常见问题（给不懂命令行的人） |
| `startup-error.txt` | 启动器缺件时用 `type` 打印的中文长说明 |
| `SHA256SUMS.txt` | exe 的 sha256，传输损坏时校验 |

**批处理内容必须纯 ASCII**（踩过的坑）：cmd.exe 在 `chcp 65001` 下按块解码批处理文件，含中文的行会在块边界被截断——实测出现「一行 `rem` 的后半截被当成命令执行」，报 `'…窗口一闪而过）' is not recognized`。中文一律放独立 `.txt` 用 `type` 输出（`type` 只吐字节，不参与解析）。`test_setup.py` 里有断言守着这条（`LAUNCHER_CMD.isascii()`）。

### 11.2 启动前自检（两层，缺一不可）

| 层 | 位置 | 能查出什么 |
|---|---|---|
| 外层 | `启动 hy-mem.cmd` | 缺 `_internal/`（此时 Python 根本没起来，内层无能为力） |
| 内层 | `cli/runtime_check.py` → `_pyi_entry._preflight()` | Windows < 8.1、32 位系统、包不完整、数据目录不可写、VC 运行库缺失、无控制台、磁盘余量不足 |

规则：

- 无参数（双击 / 首次使用）→ 完整检查；带参数 → 只做廉价必检项（3 次 stat），不拖慢高频调用。
- **只在有问题时输出**，每项都附「怎么修」（VC 运行库给 aka.ms 短链；Python 装包给华为云镜像 `mirrors.huaweicloud.com/python/`；pip 给清华源）。
- 双击场景等一次按键再退出（`runtime_check.interactive()` 判断 TTY，**智能体/管道调用绝不等待**，否则会把调用方卡死）。
- 未捕获异常统一走 `_pyi_entry._crash()`：写 `%LOCALAPPDATA%\hy-mem\logs\startup.log` + 打栈 + 双击时停窗口。静默退出是最糟的结果。
- 同一套检查也进了 `hy-mem doctor`（`sdk_import` 之后 8 项，doctor 共 19 项）。

### 11.3 为什么不用 onefile / Nuitka 兜这个场景

| 方案 | 结论 |
|---|---|
| `--onefile` | 单一 exe **350 MB**（整个目录压缩后塞进去），**每次启动都要自解压到 `%TEMP%\_MEIxxxxx`**：agent 高频调用场景下每次多 3~10 s，不可接受。且杀软对「单文件运行时释放几百个 dll」最敏感，误报率更高；`data/` 只能落 `%LOCALAPPDATA%`，删目录卸载不成立 |
| Nuitka 重编译 | 见 §17：**85% 的体积是编不动的 `.pyd`/`.dll`**，收益量级不成立 |
| **onedir + ZIP + 启动器（现状）** | 冷启 **0.4 s 量级**；接收方只需「解压 → 双击」，漏文件被启动器兜住；压缩 351 MB → 152 MB |

> 备选路线（onefile / Inno Setup / Nuitka / 体积裁剪）的触发条件与验收口径见 **§17.1**，2026-10-27 存档，**暂不做实验**。

---

## 12. 产物验收（Go / No-Go）

全 PASS ≠ 功能可用。以下必须在**打包后的 exe** 上跑：

| # | 命令 | 期望 |
|---|---|---|
| 1 | `hy-mem.exe --version` | `hy-mem 0.9.x`，中文不乱码 |
| 2 | `hy-mem.exe help` | 命令总览正常，无 traceback |
| 3 | `hy-mem.exe help --json > spec.json` | 是**单个**合法 JSON 对象 |
| 4 | `hy-mem.exe` 无参 + 非 TTY | 打简短帮助并 exit 0，**不阻塞等输入** |
| 5 | `hy-mem.exe doctor` | 11 项 PASS |
| 6 | `hy-mem.exe add "..."` → `search "..."` | 能写入、能命中（真实端到端） |
| 7 | `HY_MEM_TEST_EXE=… test_cli.py` | 68/68 |
| 8 | 干净包（无 config/data）首次运行 | 进设置向导，而不是报错 |
| 9 | 把 `dist/hy-mem-<ver>-win64.zip` 解压到**另一个目录**，双击 `启动 hy-mem.cmd` | 能进设置向导（证明压缩包自包含） |
| 10 | 只拷 `hy-mem.exe` 到空目录，双击 `启动 hy-mem.cmd` | 打印中文「运行时不完整」并**停住窗口**；不是静默退出 |
| 11 | 临时把 `_internal/chromadb` 改名后跑 `doctor` | `package` 项 FAIL，并给出「只拷了 exe」的修法 |

---

## 13. 排障速查

| 症状 | 真因 | 处置 |
|---|---|---|
| 对方双击一闪而过 / 说「打不开」 | **只拷了 `hy-mem.exe`**，`_internal/` 没跟过去（不是缺 Python、不是版本不对） | 发 `hy-mem-<ver>-win64.zip`；或整个目录一起拷。用 `启动 hy-mem.cmd` 启动，缺件时它会明确报错并停住窗口 |
| 启动器报 `'…' is not recognized`、中文变乱码 | 批处理里含中文，cmd 在 65001 下按块解码把行截断了 | 批处理保持纯 ASCII，中文放 `.txt` 用 `type` 输出（`test_setup.py` 有断言守着） |
| 「配好了却报配置不全」 | 走的是另一套环境 | `hy-mem config show` 看配置文件路径 |
| 写入后检索不到 | **入队 ≠ 入库**。`add --async` 只入队，worker 被打断（`error=interrupted`）就永远 pending | `hy-mem queue` 看 pending → `hy-mem worker --once`。要立刻可查就别用异步 |
| `search` 返回空列表 | 端点挂了，但 SDK 静默返回空 | 空结果时 CLI 会自证探测，失败走 exit 2；`--json` 带 `pending_jobs` / `hint` |
| `user_ids is required and cannot be empty` | `search` 传了 `[None]` | 已被身份三级默认修掉；自定义调用方仍应显式传 `-u` |
| `doctor` 报 `llm_live` FAIL | 探活 `max_tokens` 太小，推理模型把配额全用在 `reasoning_content`，`content` 为空 | 已改 512 + 「content 空但 reasoning 有内容也算可用」。若仍失败，看 `finish_reason` |
| `EmbeddingDimensionMismatchError` | 换过嵌入模型 | 改 `MEMORY_EMBEDDING_DIMS` 并**重建 data 目录** |
| `EMPTY_RESPONSE` + `completion_tokens == max_tokens` | 推理模型思考吃光配额 | `MEMORY_AGENT_MAX_TOKENS` 调到 3000~4000 |
| ↑↓ 没反应 / 界面假死 | DSR 探测吞键；或 mintty/管道里 `isatty()` 骗人但 `kbhit()` 永远 False | `hy-mem tui-test` 逐键自检；`HY_MEM_TUI=0` 退回编号菜单 |
| 粘贴 key 时面板出现多条 | 旧版输入行折行、只清最后一行 | 已修：`clip_input()` 水平单行窗口裁剪；长密钥永远只占一行 |
| 向导/doctor 报 `HTTP 0: UnicodeEncodeError: 'latin-1' codec can't encode ... position 7-8` | **API Key 里粘进了中文/全角/零宽**。key 进的是 `Authorization: Bearer <key>` 头，`http.client` 用 latin-1 编码，"Bearer " 占 7 字符，所以偏移量从 7 起；请求根本没发出 | 0.9.5 起**根治（三层 + 一道类型锁）**：① 输入口 `tui.ask_secret` 逐字符校验，不合格当场重问、不落盘；② 配置闸门 `readiness()` 变成 `E_CONFIG` 缺项（`add`/`search`/`doctor` 全过）；③ **`netprobe.precheck()` 在组包之前判定**（不靠异常推断），返回 `NotSent(reason)`；④ `Sent` 构造时拒绝 `status < 100`，`status=0` 这个约定已从代码里删除。文案形如「请求没有发出：向量模型密钥 第 1 位是「没」（非 ASCII 字符）」 |
| 报 `请求没有发出：域名解析失败 / 连接被拒绝 / 连接超时 / TLS 证书校验失败` | 请求没离开本机，**不是**服务器返回的错误码（`Sent` 才有 `HTTP xxx`）。`NotSent.cause` 区分 `dns` / `proxy` / `refused` / `tls` / `timeout` / `precheck` / `encoding` | 按 `cause` 判断：`precheck`/`encoding` 改配置（key、baseUrl 的字符集）；`dns`/`proxy`/`refused`/`tls`/`timeout` 查网络/代理/证书。自签证书在向导里勾「跳过证书校验」 |
| `doctor` 报 `HTTP 401: Invalid API-key provided` 但 key 是刚复制的 | 大概率**少/多复制了字符**（实测：项目 `config.json` 里 116 位 vs 可用 key 117 位，尾号不同） | 重新从控制台复制；核对时看长度而不是首尾。`HTTP 401` 旁边的中文提示就是「密钥被拒绝」 |
| 首次运行卡 30~40s 并刷 `Could not download model` | 两个叠加原因：① fastembed 默认拉 huggingface.co，国内超时重试 3 次；② `provider` 缺省曾落成 `local`，于是 `doctor` 顺手去下 92MB 模型 | 已修：`load_env()` 默认 `HF_ENDPOINT=hf-mirror.com` + 关进度条 + 摘 loguru handler；`embedding_provider()` 缺省取 config（openai）；`doctor` 遇到未缓存的本地模型只报告不下载。干净包首次 `doctor` 现为 **0.6s / 0 字节 stderr** |
| 搜索分数普遍偏低 | `min_score` 用的是 SDK 默认 0.4 | 本项目已下调 0.35（flash 的分数带窄：噪声 0.28 / 干扰 0.33 / 改写 0.355~0.372 / 直命中 0.5~0.66） |
| 一条记忆里混了多个主题 | 一次 add 被压成一条 fact，向量被拉平 | 一条只放一个主题，或 `--split`。实测：混写 0.341（低于阈值查不到）vs 拆开 0.472 / 0.641 |
| 构建「中途失败」 | 旧 exe 进程占用目录 | 关进程，或 `HY_MEM_DIST` 指到旁路目录 |
| 构建完 `--version` 是 0.1.0 | 少了 `--copy-metadata` | 见 §11 |
| `hy-mem` 在 bash 里找不到 | 只写了 `.cmd`（PATHEXT 只对 cmd 生效） | `install_ops` 已写双入口；重跑 `hy-mem install` |

---

## 14. 红线与约定

**不要做**

1. 不要 `rm -rf dist` —— 删产物用 `mv` 改名（安全保护会拦大量文件删除，且旧版 exe 可能被占用）。
2. 不要把 `config.json` / `.env` / `data/` 打进产物，也不要为了「方便」预置一份没填 key 的配置（会导致首次运行不触发引导）。
3. **不要把 `hy-mem.exe` 单独发给别人** —— 运行时在 `_internal/` 里，对方双击必闪退。对外只发 `hy-mem-<ver>-win64.zip`。
4. 启动器/批处理里不要写中文（cmd 65001 会截断行）；中文放 `.txt` 用 `type`。
5. 不要用 `--onefile`。
6. 不要改 `presets.py` 的 `dims` 而不重建数据目录。
7. 不要直接编辑宿主里的 `SKILL.md` 副本（`~/.workbuddy/skills/hy-mem/SKILL.md` 等），改 `skill/hy-mem/SKILL.md` 后 `hy-mem skill --install`。
8. 不要凭想象补 SDK 参数 —— 先用 `hy-mem help --json` 看本地实现，涉及 SDK 行为先读 `.venv/Lib/site-packages/hy_memory/` 源码。
9. **不要自己写 HTTP 调用** —— 所有出站请求只走 `cli/netprobe.py: post_json() / get_json()`。它们返回 `Sent`（带真实状态码）或 `NotSent`（带原因），`status = 0` 这个约定已从代码里删除。`test_setup.py` 有源码守卫：除 `netprobe.py` 外，`cli/` 下出现 `urlopen(` / `import urllib.request` / `requests` / `httpx`，或出现 `status == 0`，测试直接失败。回环探活（如 vdb_endpoint 的心跳）必须 `get_json(..., direct=True)` 绕代理。

**必须做**

1. 改完代码跑三套测试；动到 CLI 接口再加跑 exe 版。
2. 新增/改命令同步 `helptext.py: CMD_META`。
3. 写入路径一律包 `store.WriteLock()`。
4. 版本号只改 `cli/helptext.py: VERSION` 一处。
5. 打包前备份、打包后恢复 exe 侧 config/data（§10 Step 2 / 6）。
6. 新增任何对外请求，必须能回答「没发出时文案是什么」——不允许只处理 200 和 4xx。

**SDK 硬约束（改代码前必读，完整 14 条在 `SKILL.md` §硬约束）**

- `memory_ids` ≠ 新建节点（一次 add 返回 8~15 个 id，含被 reconcile 合并的老节点）→ CLI 用「写入前快照 + 差集」取真正新建的节点。
- `list_memories` 没有 `session_id`（只有 `session_ids`）；`search` 用复数形参、`add` 用单数。
- `search` 返回嵌套 `memories.{normal,proactive,schema}`，需拍平按 `score` 排序。
- `list_memories` 只列抽取层（l2+）；`lite` 模式写在 `l1_raw`，列表恒为空 → CLI 回退 `get_recent_history`。
- `MEMORY_PIPELINE_TRACE_ENABLED=false` 必须保留（否则退出时报 `Task was destroyed but it is pending`）。
- 配置深拷贝必须 `copy.deepcopy`（`dict(cfg)` 只复制顶层，会导致「变更检测自己和自己比」永远判不出变化）。

---

## 15. 迁移会话交接单（新会话第一件事）

按顺序执行，别跳：

```bash
cd /d/tools_open/agent-bridge

# 1. 我在哪一套环境？身份是谁？
.venv/Scripts/python.exe cli/hy_mem_cli.py config show
dist/hy-mem/hy-mem.exe config show

# 2. 环境健康吗？
dist/hy-mem/hy-mem.exe doctor          # 期望 11 项 PASS
.venv/Scripts/python.exe cli/hy_mem_cli.py doctor    # 源码侧（见 §16 已知问题）

# 3. 有没有积压没落库的记忆？
dist/hy-mem/hy-mem.exe queue
.venv/Scripts/python.exe cli/hy_mem_cli.py queue

# 4. 代码当前状态（无 git，看文件时间与版本）
grep VERSION cli/helptext.py
ls -lt cli/ scripts/ | head

# 5. 分发链是否完好（要发版才看）
ls -1 dist/ | grep -E "hy-mem|zip"

# 6. 测试基线（确认没有既存失败，再动手）
.venv/Scripts/python.exe scripts/test_ui.py
.venv/Scripts/python.exe scripts/test_setup.py
.venv/Scripts/python.exe scripts/test_cli.py
```

**读代码的顺序**（省时间）：`helptext.py`（命令全貌）→ `hy_mem_cli.py` 的 `main()` 与 `readiness()` → `config.py` → `store.py` → `menu.py` / `tui.py`（只在动 UI 时）→ `scripts/build_exe.py`（只在打包时）。

**上下文预算**：`hy_mem_cli.py` 有 2421 行，别整篇读。按符号定位：`grep -n "^def cmd_" cli/hy_mem_cli.py`。

---

## 16. 已知状态与遗留

| 项 | 状态 | 说明 |
|---|---|---|
| 源码侧嵌入 key | **401 失效** | 源码 `config.json` 里是旧 key（旧 key，**116 位**），exe 侧是新 key（新 key，**117 位**）且正常。长度差 1 位，典型是复制时被截断。所以源码 doctor 报 `embedding_live FAIL`，exe 全 PASS。**要修就把 exe 那份 key 抄到源码 config.json** |
| 数据目录位置 | 有意偏离规范 | 仍在安装根 `data/`，未改 `%LOCALAPPDATA%`（理由是「整包拷走即迁移」）。**未配置时也必须落在安装根**：`load_env()` 会把 `MEMORY_DATA_DIR` 预设成 `default_data_dir()`，否则首次运行写进 `~/.hy_memory`，向导一保存就「记忆消失」 |
| JSON 字段命名 | 有意偏离 | 部分保留 camelCase（如 `userId` / `dataDir`） |
| `hy-mem.spec` | 未使用 | `build_exe.py` 走命令行参数，不读 spec。改打包参数只改 `build_exe.py` |
| `.local-test/doctor.py` | 历史遗留、已移出仓库 | 早期 skill 漂移检测，已被 `hy-mem doctor` 取代；现放 `.local-test/`（不入库），别在里面加逻辑 |
| scipy / sklearn | 未验证能否排除 | 若排除可再省 60~100MB，但有运行期风险，需实测 |
| Hindsight 数据迁入 | 未执行 | 把外部记忆库导入 hy-mem 的一次性工作 |
| 测试残留 | 已清 | 早期探针记忆（Loki/Apollo）与验证用队列条目已删除 |
| 打包器 | PyInstaller 6.22.3 | 评估过 Nuitka，见 §17 结论「暂不换」 |
| 产物目录兼作自用安装目录 | 有风险 | `dist/hy-mem/` 既是构建输出又是自用安装目录，重打包会删其 config/data → 必须先备份（§10 Step 2） |

---

## 17. 打包工具评估（2026-10-27 结论：暂不换 Nuitka）

参考材料（千问分享的《自包含 CLI 打包流程》）建议：Python 项目优先 Nuitka，声称启动快 3~5 倍、体积小 30%+。逐条对这个项目核对后**不采纳**：

| 维度 | 事实 | 结论 |
|---|---|---|
| 启动速度 | 现产物 `--version` 冷启 **0.36~0.46 s**（视磁盘缓存）；Nuitka 最多再省 ~0.2 s | 收益可忽略。真实耗时在 import 之后的网络调用（嵌入 300 ms、抽取 5~40 s） |
| 体积 | **实测 350.9 MB 中 298.8 MB（85%）是 `.pyd`/`.dll`/词典数据**，纯 Python 仅 52.1 MB（15%）。见 §17.1 | 能省的只有那 15% 的一部分，实测量级不变（仍 300+ MB） |
| 动态导入 | chromadb 大量模块按名字运行时加载（这正是必须 `--collect-submodules chromadb` 的原因）；`hy_memory` 同理 | Nuitka 静态分析会漏，需逐个 `--include-module`；**失败模式是「构建成功、运行才炸」**，本项目已踩过一次 |
| 构建时间 | 编译 C 依赖树 + 8.5k 行自有代码，预估 20~40 分钟 | 迭代成本高 |
| 交叉编译 | 两者都不支持，只能各平台构建 | 无差别（只发 Windows） |
| 单文件 | Nuitka 原生支持 onefile | 但 onefile 的痛点是「每次启动解包」，与编译器无关 |

**结论**：这次闪退的根因是**分发形态**（只发了 exe），不是编译器。把已验证可行的构建链换成未验证的，风险大于收益。

**什么时候值得重新评估**：① 需要 Linux/macOS 产物又不想维护多套构建；② 冷启动成为真实瓶颈（如被 agent 每秒调用）；③ chromadb / onnxruntime 换成纯 Python 实现。届时先做 spike：Nuitka 编译后跑通 `add` + `search` 端到端，再谈迁移。

### 17.1 备选路线记录（2026-10-27 存档，**暂不做实验**）

用户拍板：路线先记录，占时不做实验。**当前构建链不动**，以下四条仅作为将来需求变化时的备选，触发条件写明，避免下次又从头论证。

先看决定一切的体积事实（`dist/hy-mem-0.9.5-win64.zip` 实测）：

```
解压总量        350.9 MB
├─ 二进制+资源  298.8 MB  (85%)   .pyd 210.9 + .dll 73.7 + 词典/数据 14.2
│                                  └─ Nuitka 编译不掉，原样搬运
└─ 纯 Python     52.1 MB  (15%)   ← 唯一可被编译器影响的部分
```

`.pyd` 大头：chromadb Rust 绑定 60.5、scipy 46.6、onnxruntime 37.5、numpy 20.2、PIL 12.9、sklearn 11.8、kuzu 13.0、grpc 12.1。**任何编译器都改不了这些**，所以"体积小 30%+"在含重二进制依赖的项目上不成立。

| 备选路线 | 触发条件（出现才考虑） | 代价 / 风险 | 预估工作 |
|---|---|---|---|
| **A. `--onefile` 单 exe** | 用户明确要求"就一个文件、慢点无所谓"（如只能发单个附件的场景） | 每次启动自解压 350 MB → 冷启数秒且**每次运行都解**；磁盘 ×2；杀软启发式最敏感的行为；`data/` 只能落 `%LOCALAPPDATA%`，"删目录即卸载"失效；解压失败/ TEMP 只读 → 仍是"双击闪退" | 半天：加 `HY_MEM_ONEFILE=1` 分支 + 实测启动耗时 + 冒烟。**必须作为附加产物，不替换 zip** |
| **B. Inno Setup 安装器** | 要"点一下装好 + 开始菜单 + 卸载入口 + 自更新" | 需管理员权限、写注册表、卸载留残留；杀软对安装器更敏感；失去便携性 | 1~2 天：iss 脚本 + 签名（可选）+ 升级逻辑 |
| **C. Nuitka 全量重编译** | ① 冷启成真实瓶颈；② 依赖里的重二进制（chromadb/onnxruntime）换成纯 Python；③ 要出 Linux 产物 | 本机**无 `cl.exe`**，需先装 VS Build Tools（数 GB）；构建 3 分钟 → 数十分钟；动态导入需逐个 `--include-package`，**失败模式是"构建绿、运行炸"**；体积只降 20~40 MB | 3~5 天（含 268 用例全部重跑 + 端到端 spike） |
| **D. 只做体积裁剪（不改工具链）** | 要"能发微信"（<120 MB） | 有一定风险动到运行期依赖：须逐个验证 | **半天，优先级最高性价比**：裁 `_internal/sklearn/`（11.8 MB，含 openml 测试数据）、`*.lib`/`*.pyx`/`*.pxd`（构建期文件，运行不需要）、`datasets/tests` 数据、`langdetect` 多余语言模型 |

**若将来做 A（onefile）实验，验收口径固定为**：① `--version` 连续 5 次取均值/最大值（含首次冷启）；② `%TEMP%` 落盘体积；③ 268 用例全跑；④ 端到端 `add`→`search`；⑤ 与现产物体积/启动对比表。不做这五项，不算实验完成。

**做 D 之前必须**：裁剪后用 `server` 起一次、跑 `add`+`search`+`worker --once`，因为 sklearn 是 chromadb 的隐式依赖，剪错会在运行期才炸。

---

## 附：常用一行命令

```bash
# 源码跑 CLI
.venv/Scripts/python.exe cli/hy_mem_cli.py <cmd>          # 或 hy-mem <cmd>（PATH 上指向源码）

# 交互界面 / 设置
hy-mem                                                    # TTY 下进主界面
hy-mem setup                                              # 四步向导
hy-mem --help-json | python -m json.tool                  # 机器可读规格

# 记忆操作
hy-mem add "一条一个主题" --tag 项目A                     # 同步写（pro 模式 8~40s）
hy-mem add "..." --async && hy-mem worker --once          # 异步写 + 立刻消费
hy-mem search "自然语言问法" --json
hy-mem stats --health                                     # 库体积与延迟健康度

# 分发（对外只发 zip，不发单个 exe）
.venv/Scripts/python.exe scripts/build_exe.py      # → dist/hy-mem/ 与 dist/hy-mem-<ver>-win64.zip
# 接收方：解压 → 双击「启动 hy-mem.cmd」

# 维护
hy-mem doctor
hy-mem install                                            # 生成 PATH 启动器
hy-mem skill --install                                    # 铺 SKILL.md 到 5 个宿主
hy-mem tui-test                                           # 方向键/鼠标自检
```
