# PRD · hy-mem 端点链路缺陷批量修复（0.9.10）

> 提出人：胡超　｜　记录：阿衡　｜　日期：2026-10-28
> 触发事故：TUI 内「检索 → 写入 → 立即消费 → 再检索」直接报 `Could not connect to a Chroma server`，此后该会话内**所有**命令都不可用。
> 处理方式（按你的要求）：**本轮只测不修**。先把功能点全部跑一遍，缺陷集中记在这里，之后一次性修 + 一次性打包。
> 状态更新（同日 15:00）：**已按 B1–B4 一次性修完并打包 0.9.10**，逐条对照见 §7。

---

## 0. 一句话结论

`0.9.9` 引入的「worker 独占 chroma server + 客户端走 HttpClient」方向正确，但**生命周期设计错了**：把「数据所有者（必须长驻、必须单例）」和「队列消费者（天然短命、跑完就退）」塞进同一个 `worker` 命令，又让客户端把它当作「可随时拉起、可静默降级」的资源。三条链路互相踩：消费完杀 server → 端口文件被删 → 客户端 15s 等不到端点 → 静默回落嵌入式 → 读到过期索引。

---

## 1. 测试范围与方法

| 项 | 内容 |
|---|---|
| 环境 | 安装版 `D:\tools_open\hy-mem-0.9.9-win64`（exe 构建时间 13:34） |
| 数据隔离 | 拷贝真实库到 `D:\tools_open\tmp-hymem-sweep\data`，配 `HY_MEM_CONFIG` 指向沙箱配置（`storage.dataDir` 显式写死） |
| 巡检器 | `scripts/probe_cli_sweep.py`（22 项命令级用例，脚本托管 worker，避免沙箱收割进程） |
| 交互态复现 | `scripts/probe_endpoint_lifecycle.py`（同进程内 `search → worker --once → search`，等价 TUI dispatch） |
| MCP 链路 | `scripts/probe_mcp_stdio.py` |
| 覆盖面 | 29 个子命令中的读路径全部覆盖；写路径覆盖 add/sync、add/async、worker、tag、untag、update、delete、drain、retry；MCP 全部工具；端点冷启/热路径/宿主被杀三种状态 |
| 报告 | `%USERPROFILE%\hymem_sweep.json`（原始 stdout/stderr/rc/耗时） |

**巡检结果：22 项中 3 项 FAIL，另有 5 项在定向复现中暴露（下表合计 12 个缺陷，含 3 个 P0）。**

---

## 2. 缺陷清单

### F1（P0）交互进程内跑 worker，把本进程的端点指向死端口

| 项 | 内容 |
|---|---|
| 现象 | TUI 检索一次成功 → 写入后选「现在就跑一次 worker 落库」→ 再检索报 `ValueError: Could not connect to a Chroma server`，此后列表/详情/统计全部同样报错，只能重启 CLI |
| 复现 | `scripts/probe_endpoint_lifecycle.py`：`rc=2`，`env after worker: 127.0.0.1:58033`，而 `server.port` 已被删除 |
| 触发链 | ① `hy_mem_cli.py:2445` `dispatch = lambda argv: main(argv)` → 子命令在**同一进程**执行；② `cmd_worker` 起自己的 chroma server、`os.environ["MEMORY_VECTOR_PORT"]` 覆盖成新端口；③ 退出时 `stop_server_thread` 删端口文件、停线程，但父进程 env 不改；④ 下次 `ensure_endpoint` 在 `vdb_endpoint.py:245` 见 `MEMORY_VECTOR_HOST` 有值直接 `return None`（当成"外部配置"），不复验 |
| 影响 | 长驻进程（TUI / MCP stdio）一旦踩中即**永久坏掉**，用户侧表现为"整个 CLI 挂了" |
| 修复方向 | ① 交互进程不再进程内跑 worker（dispatch 改子进程）；② `ensure_endpoint` 对"自己注入的 env"每次复验心跳，失败即清 env 重拉；③ `worker` 只在真正当宿主时起 server（见 F5） |

### F2（P0）worker 退出无条件删除端口文件，不校验 owner

| 项 | 内容 |
|---|---|
| 现象 | 宿主 worker 仍在运行，但 `server.port` 被另一个短命 worker 删掉；随后所有检索耗时 19s 并回落嵌入式 |
| 复现 | 宿主 pid 28368 存活、端口 `{"port":58034,"pid":28368}` → 跑一次 `worker --once`（2.1s 退出）→ 端口文件**消失** → 检索 19.4s，stderr：`未能获得 chroma server 端点，回落嵌入式向量库` |
| 触发链 | `cmd_worker` 的 `finally` 调 `stop_server_thread()`，内部 `remove_port_file()` 无 pid 归属判断；而它启动时已用 `write_port_file()` 覆盖了宿主的登记 |
| 影响 | 每次「立即消费」都会打掉端点；CLI 每次检索多付 15s 轮询 + 结果可能过期 |
| 修复方向 | 端口文件带 `owner_pid`；删除前比对自身 pid；`--once` 不写/不删端口文件（见 F5） |

### F3（P0）回落嵌入式不是安全降级

| 项 | 内容 |
|---|---|
| 现象 | 端点不可用时静默走嵌入式，检索返回 0 条或旧值，且 `rc=0`，调用方无法察觉 |
| 证据 | 巡检「search 新记忆」：19.8s、0 命中、stderr 仅一行提示；同一查询在宿主存活时 4.8s 正常（数据在库：`get 76ce77b8` 有内容） |
| 影响 | 比直接报错更坏——智能体拿到"空记忆"会据此编答案。原始 2026-10-28 事故（`pool_size=30, returned=0`）同源 |
| 修复方向 | 端点拿不到时**明确失败**（`E_RUNTIME` + 修复提示）；仅 `--embedded` 显式指定才走嵌入式 |

### F4（P1）ensure_endpoint 把自身注入的 env 当外部配置，永不复查

| 项 | 内容 |
|---|---|
| 现象 | 进程内端点一旦失效，后续所有调用都不再尝试复活 |
| 证据 | `vdb_endpoint.py:245-246`；probe 第 4 步之后即使重新拉起 worker 也不恢复 |
| 影响 | MCP stdio（常驻一个进程服务整个会话）、TUI 只要踩一次就全废 |
| 修复方向 | 注入时打标记（如 `MEMORY_VECTOR_HOST_FROM=hymem`）；有标记 → 每次心跳复验；无标记（用户手配远程 chroma/qdrant）→ 不动 |

### F5（P1）`worker --once` 也启动 chroma server（第二数据所有者）

| 项 | 内容 |
|---|---|
| 现象 | 短命消费者抢着当 server 宿主，违反"数据唯一所有者"约束 |
| 证据 | `worker_sweep.err` 出现 `chroma server 就绪（:49230）`（这正是宿主端口，被 --once 覆盖）；安装日志累计 15 次 `就绪`，端口 58003/63283/58371/58034 各异 |
| 影响 | 同目录多 server 实例；配合 F2 造成端口文件错乱；极端情况索引互踩 |
| 修复方向 | server 只在"宿主模式"启动（新增 `hy-mem serve` 或 `worker --serve`）；`worker --once` 纯 HttpClient 消费者 |

### F6（P1）`MEMORY_DATA_DIR` 在配置文件存在时被静默忽略

| 项 | 内容 |
|---|---|
| 现象 | 设了 `MEMORY_DATA_DIR=<沙箱>` 的进程，数据仍写进安装目录 |
| 证据 | `cli/config.py:210-214`：`_fill_from_env` 只在**配置文件不存在**时执行；随后 `apply_env` 用配置里的 `dataDir`（默认安装根）覆盖环境变量。实测：`MEMORY_DATA_DIR` 指向沙箱时，worker 把 `server.port` 写进 `D:\tools_open\hy-mem-0.9.9-win64\data` |
| 影响 | 无法做沙箱/多实例/备份校验；测试直接污染真实记忆库（本轮第一版巡检就踩了） |
| 修复方向 | 环境变量优先级高于配置文件（至少 `MEMORY_DATA_DIR`），并在 `doctor` 里显示"来源：env / config / 默认" |

### F7（P1）`add --tag` 的用户标签不落库

| 项 | 内容 |
|---|---|
| 现象 | 写入时带的标签丢失，只剩 LLM 自动标签；`list --tag` / `search --tag` / `untag` / `archive` 全部失效 |
| 证据 | 同步 `add --tag 冒烟巡检` → 返回 `651f4bc8`（l1_raw，`tags=[]`）；检索命中 `288471d6`（l2_fact，`tags=['hy-mem','testing','smoke-test']`，无「冒烟巡检」）；`list --tag 冒烟巡检` → `count=0`；`tags` 统计里也没有该标签 |
| 影响 | 标签体系整体不可用（归档、过滤、批量操作依赖它） |
| 修复方向 | 用户标签在 l1 落库并透传到 l2；自动标签与之合并而非替换；补回归用例 |

### F8（P1）`add` 返回的 memory_id 是影子节点，与可检索节点不一致

| 项 | 内容 |
|---|---|
| 现象 | `add` 返回 l1_raw（`status=shadow`）id；检索/list 返回 l2_fact id |
| 证据 | 同 F7：`651f4bc8`（l1_raw/shadow）vs `288471d6`（l2_fact） |
| 影响 | 用户/智能体拿 add 的 id 去 `get` 只看到影子内容、去 `delete` 未必命中可检索节点；`update` 也会指错 |
| 修复方向 | add 返回可检索节点 id（或同时返回 `{l1_id, memory_id}`）；`get/delete/update` 支持任一 id 解析到同一链 |

### F9（P2）异步写入偶发停在 l1_raw/shadow，job 却标 done

| 项 | 内容 |
|---|---|
| 现象 | job 84 日志 `add ok 26298ms`，但记忆 `76ce77b8` 为 `layer=l1_raw, status=shadow, tags=[]`，检索 0 命中 |
| 证据 | `get 76ce77b8`；`queue --status done` 显示 job 84 done + memory_ids 有值；同步路径同类内容能落成 l2_fact |
| 影响 | "写进去了但永远搜不到"，且队列显示成功，无法自愈 |
| 修复方向 | 抽取结果为空时不应标 done（参照 `EMPTY_RESPONSE` 已有重试）；或落 l1 后补一次后台抽取并暴露状态 |

### F10（P2）拉起闸门 15s 窗口 + 硬杀残留行，导致 15s 内全量降级

| 项 | 内容 |
|---|---|
| 现象 | 宿主被强杀后，紧接着的检索 19.4s 且回落嵌入式、端口文件未重建 |
| 证据 | 巡检「search 宿主死后（应自愈）」FAIL；`store.claim_worker_slot(fresh=15.0)` 只按心跳时间清行，被 SIGKILL 的 worker 行要等 15s 过期 |
| 影响 | 崩溃/强退后有一段"必然降级"窗口，叠加 F3 就是"静默读旧数据" |
| 修复方向 | 拉起闸门对"pid 已不存在"的注册行立即清理（Windows 用 `OpenProcess` 探活） |

### F11（P2）安装版落后于源码，测试对象不是最新代码

| 项 | 内容 |
|---|---|
| 现象 | 安装目录 exe 为 13:34 构建，缺 `queue_status` 工具、多行折行渲染、MCP `worker_live`；`dist/hy-mem/` 在 14:22 被清空后未重新构建成功 |
| 证据 | MCP `tools/list` 只返回 7 个工具（源码已 8 个）；`dist/` 仅剩 zip 与 `_hy-mem-old-142245` |
| 影响 | 你今天在 TUI 看到的仍是截断版渲染；所有验收结论都要标注"针对 13:34 构建" |
| 修复方向 | 修完本批缺陷统一打包 + 装到 0.9.10 目录；打包脚本增加"产物与源码一致性"自检（比对 `VERSION` 与 `git rev-parse`） |

### F12（P3）`data/` 被纳入版本控制

| 项 | 内容 |
|---|---|
| 现象 | 每次运行 CLI 后 `git status` 都脏（今天 9 个 data 文件 + `-shm/-wal` 未跟踪） |
| 证据 | `git status --short`：`M data/data/vector_db/...`、`?? data/hy_mem_queue.db-shm` 等 |
| 影响 | 与 AGENTS「产物/环境/缓存不入库」自相矛盾；误提交会带真实记忆与密钥线索 |
| 修复方向 | `git rm -r --cached data` + `.gitignore`（需你确认后执行，属破坏性 git 操作） |

---

## 3. 设计问题：自动拉起 worker 是不是过度了？

**自动拉起本身不过度；过度的是"一个 worker 背两种生命周期"。**

| 角色 | 天然属性 | 现状 |
|---|---|---|
| 队列消费者 | 短命：有活干活，空闲 30s 退 | ✅ 合理（`add --async` 毫秒返回靠它） |
| chroma server 宿主 | 长驻、单例、独占数据目录 | ❌ 被塞进同一个 `worker`，跟着消费者一起退 |

于是出现三个自相矛盾：
1. 消费完就退 → 顺手把 server 带走（F2/F5）；
2. 客户端既要"随时能拉起"，又想"拿不到就降级"→ 静默读旧数据（F3）；
3. TUI 用同进程 dispatch → 同进程起第二个 server（F1）。

**收敛方案（建议，最小改动，不做则永远在补窟窿）**
1. 拆命令：`hy-mem serve`（宿主，长驻单例，端口文件带 owner）／`hy-mem worker`（消费者，纯客户端，永不起 server）；
2. 客户端 env 注入带来源标记，自己注入的必复验；端点不可用**直接失败**，不静默降级；
3. TUI 一律子进程调子命令，交互进程不承载任何后台角色。

代价：多一个子命令、端口文件多一个 owner 字段、`ensure_endpoint` 多一条复验分支。相比现在"随机坏一次、只能重启"的体验，这个代价必须付。

---

## 4. 修复批次计划（下一轮一次性做完）

| 批次 | 内容 | 关联 |
|---|---|---|
| B1 端点骨架 | serve/worker 角色拆分 + owner 校验 + 复验 + 不静默降级 | F1 F2 F3 F4 F5 F10 |
| B2 数据目录 | `MEMORY_DATA_DIR` 优先级 + doctor 显示来源 | F6 |
| B3 标签与节点 | 用户标签透传 + add 返回可检索 id + 影子态自愈 | F7 F8 F9 |
| B4 交付 | 重打包、装 0.9.10、TUI/MCP/CLI 全量回归、产物一致性自检 | F11 |
| B5 仓库卫生 | data 移出版本控制（**需你点头**） | F12 |

**回归门槛**：`probe_cli_sweep.py` 22/22 全绿 + `probe_endpoint_lifecycle.py` PASS + `probe_mcp_stdio.py` 8 工具全通 + `scripts/test_*.py` 全绿 + 交互手工走一遍「检索→写入→立即消费→再检索」。

---

## 5. 测试盲区（为什么 324 项单测全绿，产品还是坏）

同一份源码跑 `test_ui` 92 + `test_cli` 84 + `test_vdb_endpoint` 23 + `test_setup` 103 + `test_mcp` 22 = **324 项全绿**，但真实故障 3 个 P0 全部逃过。原因：

1. **没有"状态链"用例**：所有用例都是「一次调用一个进程」，从不验证「同一进程先检索、再跑 worker、再检索」；
2. **不验证 owner 语义**：`worker --once` 删掉别人的端口文件，单进程测试看不出来；
3. **把降级当成功**：回落嵌入式时 rc=0，用例只看 rc；
4. **测试数据目录不可隔离**（F6），所以真实库的陈旧索引问题在测试里不会出现。

→ B1 批次必须补的契约测试：① 同进程状态链；② 双 worker 端口文件归属；③ 端点丢失必须非零退出；④ `MEMORY_DATA_DIR` 生效。

---

## 6. 附：复现命令

```bash
# 交互态（F1）：同进程 search → worker --once → search
HY_MEM_CONFIG=D:\tools_open\tmp-hymem-sweep\config.json \
  .venv/Scripts/python.exe scripts/probe_endpoint_lifecycle.py "胡超是谁"

# 功能点巡检（F3/F6/F7）
HY_MEM_CONFIG=D:\tools_open\tmp-hymem-sweep\config.json \
  .venv/Scripts/python.exe scripts/probe_cli_sweep.py

# MCP 工具数（F11）
HY_MEM_CONFIG=D:\tools_open\tmp-hymem-sweep\config.json \
  .venv/Scripts/python.exe scripts/probe_mcp_stdio.py
```

沙箱数据：`D:\tools_open\tmp-hymem-sweep\data`（真实库的拷贝，可随时删除重来）。

---

## 7. 修复记录（0.9.10，一次性交付）

批次与 PRD §4 一致；每条都写明改在哪、怎么验。**B5（data 移出版本控制）未执行**——属破坏性 git 操作，等你点头。

| 缺陷 | 修法（落点） | 验收 |
|---|---|---|
| F1 交互进程内跑 worker | `menu.subprocess_dispatch`：子命令一律子进程（`cli/menu.py`），`cmd_menu` / 无参入口都换过去 | `probe_endpoint_lifecycle.py` 第 3 步「父进程 env 未被污染 PASS」 |
| F2 端口文件无差别删除 | `server.port` 带 `{port,pid,owner}`，`remove_port_file(expect_pid=…)` 只删自己的；`stop_server_thread` 必带本进程 pid | `test_vdb_endpoint`「别人的 pid 删不掉我的登记」「停 server 不误删别人的登记」 |
| F3 静默回落嵌入式 | 主人活着但连不上 → 抛 `EndpointUnavailable`（`E_ENDPOINT`/exit 2，`cli/errors.py` 给专属 hint）；只有「从无主人 + 拉起失败」才允许回落且 stderr 告警 | `test_vdb_endpoint`「主人活着但连不上 → 抛 EndpointUnavailable」「失败时不注入 env」 |
| F4 自注入 env 不复验 | 注入时打 `MEMORY_VECTOR_HOST_SRC=hymem`；`ensure_endpoint` 见标记就每次复验心跳，失效即清 env 重取；无标记（外部配置）不动 | `test_vdb_endpoint`「自注入端点失效 → 清 env 并重新获取」 |
| F5 `worker` 兼当 server 宿主 | 新增 `hy-mem serve`（长驻单例、空闲自退、抢占登记）；`cmd_worker` 删掉起 server 与停 server 的代码，退回纯消费者 | `probe_cli_sweep`「serve 幂等 PASS」；`worker --once` 不再写/删端口文件 |
| F6 `MEMORY_DATA_DIR` 被配置覆盖 | `config.load()` 让 `MEMORY_DATA_DIR` **无条件**压过配置文件；新增 `data_dir_source()`，`doctor` 的 `data_dir` 项显示来源 | `probe_cli_sweep`「version --json（数据目录隔离）PASS」（沙箱 dataDir 生效） |
| F7 `add --tag` 标签丢 | 新增 `_bind_tags_to_new()`：写入前后 `list_memories` 差分，把标签绑到**真正新建的可检索节点**；同步/异步两条路径都换掉 | `probe_cli_sweep`「list --tag（用户标签可见）count=1 PASS」 |
| F8 `add` 返回影子 id | 返回体 `memory_id` 一律指向 `memory_ids[0]`（可检索节点），原 l1 id 留在 `raw_memory_id`；`get` 读影子节点时附提示 | 人工 `add` 后 `get <memory_id>` 直接命中 l2_fact |
| F9 影子态静默成功 | `_execute_job` 检测「无 memory_ids（只落 l1_raw）」→ stderr 告警 + `jobs.error='shadow_only…'`；`queue` 顶部提示计数；新增 `hy-mem retry --shadow` 一键重投 | `queue` 输出含 `shadow_only`；`store.requeue_shadow()` |
| F10 闸门 15s 空窗 | `claim_worker_slot` 先 `_purge_dead_pids`（只对 3s 以上的行探活，Windows `OpenProcess` / POSIX `kill-0`），被强杀的 worker 立即腾出名额 | `probe_cli_sweep`「search 宿主死后（应自愈）rc=0 6.0s，回落嵌入式=False」 |
| F11 安装版落后源码 | 重打包 0.9.10；装到 `D:/tools_open/hy-mem-0.9.10-win64`（0.9.9 目录保留为备份）；`~/.local/bin` 与 `%LOCALAPPDATA%\hy-mem\bin` 两处启动器都改指 0.9.10 | `hy-mem version` → 0.9.10；`tools/list` 8 个工具 |
| F12 `data/` 入库 | **未执行**，需你授权 `git rm -r --cached data` + `.gitignore` | — |

### 修复后的端点语义（一句话）

`serve` 是数据唯一所有者（长驻单例、空闲 30 分钟自退、登记带 owner）；`worker` 只是队列消费者，永不起 server；客户端每次复验自己注入的端点，主人活着却连不上就**明确失败**，绝不静默读旧索引。

### 本轮回归

| 套件 | 结果 |
|---|---|
| `test_vdb_endpoint.py` | **31/31**（原 23，新增 owner 校验 / 六个 ensure 分支 / 自注入复验） |
| `test_cli.py` | 84/84 |
| `test_queue.py` | 18/18 |
| `test_ui.py` | 92/92 |
| `test_setup.py` | 103/103 |
| `test_mcp.py` | 22/22（含 add → 后台 worker → 搜索全链路） |
| `probe_endpoint_lifecycle.py` | 6/6 PASS（含「杀 serve 后自愈」） |
| `probe_cli_sweep.py` | 见下（对 0.9.10 安装版） |
| `probe_vdb_http.py` / `probe_mcp_stdio.py` | 见下 |
