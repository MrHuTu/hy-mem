# 2026-10-28 · CLI 体验修复轮（0.9.11）

> 提出人：胡超　｜　记录：阿衡　｜　版本：0.9.10 → **0.9.11**
> 入口：TUI 写记忆报 `UnicodeEncodeError: ... '\udcaa' ... surrogates not allowed`（截图见会话），
> 并附四个改进诉求：连续写入 / 操作日志 / 中英切换 / 基座 SDK 是否同步。

---

## 1 图上的 bug：stdin 编码链路（已修，有回归闸门）

### 1.1 现象

TUI「写记忆」输入中文 → `写入中…` → 直接失败，并把责任推给了密钥：

```
× hy-mem: UnicodeEncodeError: 'utf-8' codec can't encode character '\udcaa'
  in position 17: surrogates not allowed（密钥或地址里含非 ASCII 字符
  （中文/全角/零宽），只能是 ASCII；重跑 hy-mem setup --step embedding 重新填一次）
```

按提示去改密钥不会有任何效果 —— 因为密钥根本没参与这次失败。

### 1.2 根因（实测，不是推断）

冻结进程（PyInstaller）的 `sys.stdin` 编码是**系统 locale（中文 Windows 上 cp936）**，
错误处理是 **surrogateescape**；而上游（TUI 子进程派发、`type note.txt |`、脚本）按
**UTF-8** 喂字节。cp936 把这些字节切错，剩下的零头被记成孤立代理项 `\udcXX`：

```python
>>> '刘德华是个歌星，香港歌手。'.encode('utf-8').decode('gbk', 'surrogateescape')
'鍒樺痉鍗庢槸涓\udcaa姝屾槦锛岄\udca6欐腐姝屾墜銆\udc82'
```

代理项一路活到「建 HTTP 请求体」那一步才炸（`json.dumps(...).encode('utf-8')`），
所以报错位置（position 17）离真正的输入处隔了好几层，错误归类也跟着跑偏。

复现命令（改前必炸，改后必过）：

```bash
printf '刘德华是个歌星，香港歌手。' | hy-mem add --async
```

### 1.3 修法

| 落点 | 改动 |
|---|---|
| `cli/hy_mem_cli.py: _read_text` | 不再用 `sys.stdin.read()`，改为读 `sys.stdin.buffer`；解不走 locale，走**显式链**：BOM → UTF-8 → GBK → `replace` 兜底（`_decode_stdin`） |
| `cli/hy_mem_cli.py: _strip_surrogates` | 最后一道闸：无论从哪来，孤立代理项在拼请求体之前就被剔除，并在 stderr 说清剔了几个 |
| `cli/errors.py: classify_error` | `UnicodeEncodeError` 按消息分流：含 `surrogate` → 报 `E_ENCODING`，指向**输入侧**（改 `chcp 65001` 或参数直传）；其余（latin-1/position N）才指向密钥/地址 |

**为什么不做成「reconfigure stdin 成 utf-8」**：那样 GBK 管道（`cmd.exe` 老习惯）会变成
一堆替换字符 —— 静默乱码比报错更糟。按字节读 + 解码链能同时吃 UTF-8 与 GBK，且猜错时打警告。

### 1.4 验收

- 新增 `scripts/test_encoding.py`（10 项）：UTF-8 / GBK / BOM / UTF-16 / 二进制垃圾 / 空输入 /
  代理项剔除 / **事故环境复刻**（`PYTHONIOENCODING=gbk:surrogateescape` 子进程跑 `_read_text`，
  内容必须逐字正确且 stderr 不得出现 `surrogates not allowed`）。
- 手工：UTF-8 管道、GBK 管道、复刻冻结环境三种条件下写入，队列 payload 均为逐字正确的
  `刘德华是个歌星，香港歌手。` / `张学友也是歌手，香港的。`。

---

## 2 写记忆支持连续写入

**改动**（`cli/menu.py: page_add`）：一次会话内循环写，写完一条问「再写一条？」（默认 Y），
N / Esc / Ctrl+C 才回主界面。

三个刻意的细节：

- **循环之间不清屏**：上一条的「已入队 / 已落库」必须留在屏幕上 —— 「写完到底进没进库」
  是这个工具被问得最多的问题。
- **失败也继续问**：一条写坏了不该把「刚想好的另外三条」冲掉。
- **取消与手滑分开**：输入阶段按过 Esc/Ctrl+C 的，写完这条就退出，不再追问；敲空行只是没输入，
  仍会问「再写一条？」。

标题按轮次显示「写记忆（第 2 条）」，让人知道还在同一个会话里。

---

## 3 操作日志（只保留最近 50 条）

**为什么要有**：失败经常发生在子进程里（交互界面每次操作都是一次子进程调用，异步写入还要等
worker），回头想「刚才到底跑了什么」时，终端滚动缓冲早没了。

**落点**：`<storage.dataDir>/ops.log`，纯文本一行一条，人能直接读：

```
2026-10-28 16:02:38  add         FAIL      0.0s  E_USAGE --metadata 必须是合法 JSON 对象：Expecting property name…
2026-10-28 16:05:11  add         OK        3.2s  写记忆 1 条 · id=8f3a1c…
```

| 设计点 | 取舍 |
|---|---|
| 记录位置 | `hy_mem_cli.main()` 统一计时 + 记退出码 → CLI 与 TUI（子进程调 CLI）一次覆盖 |
| 只留 50 条 | 追加后超限即截断保留尾部 50 行，临时文件 + `replace` 原子写 |
| 不变明文副本 | `add/update/search` 的正文与查询词**不落盘**（只记 id / 标签 / 条数这类定位信息） |
| 密钥打码 | 键名含 key / token / secret / password → 值打成 `***` |
| 绝不连累命令 | 写失败静默放弃（`--verbose` 才在 stderr 说一句），不改退出码 |

**查看**：`hy-mem log [-n 50] [--clear] [--json]`；交互界面「设置 → 操作日志」也能看与清空。

---

## 4 中英文切换

**入口**：`hy-mem config set lang=en`（立即生效）｜`HY_MEM_LANG=en hy-mem …`（临时覆盖）｜
设置页「界面语言 / Language」。优先级：`HY_MEM_LANG` > `config.lang` > `zh`。

**实现**（`cli/i18n.py`）：以**中文原文为 key**，`tr("写记忆")` 在 en 时查表返回 `Write memory`，
**查不到就原样返回中文** —— 少翻一句不该让界面崩，也不该露出 `msg_id`。

**覆盖面**（本轮按约定范围）：交互界面全部页面与菜单、`errors.py` 的三类提示、帮助文本的环境变量
与 `log` 命令条目、`config set lang` 说明。命令行其余子命令的正文输出仍为中文 —— 这是明确取舍，
不是遗漏：`test_cli` 等测试断言了大量中文字面量，默认 zh 必须逐字不变。

---

## 5 附带修掉的真问题：端口登记的 pid 被复用

跑回归时 `test_cli` 突然 3 项失败，报：

```
EndpointUnavailable: 向量库服务（pid 209972, :59759）进程还在但连不上心跳
```

查下去：登记里说 pid 209972 是 serve，**实际那是个 node 进程** —— 被强杀的 serve 留下的登记，
pid 被 Windows 回收后分给了别人。只看 `pid_alive` 就认定「主人活着」，心跳当然连不上，于是
一整轮命令全废。这与 PRD §3 那类「一个会话全废」是同一个坑的另一个入口。

**修法**（`cli/vdb_endpoint.py: pid_reused`）：用**进程创建时间**做身份校验 —— serve 登记
`started_at` 时自己早就起来了，所以 `创建时间 <= started_at`；反过来就说明 pid 已易主
（Windows 走 `OpenProcess + GetProcessTimes`，POSIX 读 `/proc/<pid>/stat` 的 starttime + btime）。
判不了（权限不足 / 老格式登记）就**放行**，绝不误杀活着的 serve。

验收：`test_vdb_endpoint` 31 → **36**（新增「老登记不判复用」「同一进程不判复用」「pid 已易主」
「复用的登记不抛 EndpointUnavailable」「复用的登记被清掉」）。

---

## 6 基座 SDK：hy-memory 2.0.5 → 2.0.6 是否同步

### 6.1 结论

**建议升，但不急**：2.0.6 对本项目是**纯增量 + 一处语义修复**，零依赖变化、零调用面破坏；
实测 227 项 SDK 相关测试全绿。建议与本次功能一起打包升级（一次打包，不额外增加验证轮次）。

### 6.2 证据

| 维度 | 结果 |
|---|---|
| 上游发布 | `2.0.6` 发布于 2026-10-27（`CHANGELOG.md`）；`2.0.7` 尚在准备，**未发 PyPI** |
| API 签名 | 5 个方法有差异，**全部是新增的关键字可选参数**：`search(rerank=True)`、`list_memories(scene=None)`、`update(kind/variant/task)`、`delete(kind/variant)`、新增 `memory_stats()`。无位置参数变化，无删除 |
| 依赖 | `Requires-Dist` 33 条**完全一致**，无新增、无移除、无版本抬高 |
| 实测（同套测试换 2.0.6） | `test_cli 84/84`、`test_queue 18/18`、`test_setup 103/103`、`test_mcp 22/22`（含 add → worker → search 全链路） |

### 6.3 值得升的那一条

2.0.6 把「reconcile 保持时间序」从**提示词要求**变成了**代码约束**
（`hy_memory/agent/reconciler.py`：`[reconciler] blocked: memory_at ... is older than existing ...`）。
旧版只靠 prompt 约束 LLM，老记忆仍可能覆盖新记忆 —— 而我们展示的正是事实的
Current / When / History 演化链，覆盖错了就是「记忆被静默改旧」。

### 6.4 升级后的行为差异（都不影响正确性）

| 差异 | 我们的影响 |
|---|---|
| MMR 多样性选择只在 `limit < 20` 时跑 | `-n` 默认 10 不受影响；带 `--tag` 时 `fetch_limit ≥ 30` 会跳 MMR —— 但我们自己按 score 重排，观感无变化，且更快 |
| `update/delete` 跨场景同 id 抛 `AmbiguousMemoryIdError` | 我们只操作 chat 记忆，不会同 id 多场景；真撞上会落到通用 `E_RUNTIME` 提示（可后续补一条专属文案） |
| `list_memories` 的 `total` 改为真实总数 | 我们从不读 `total`（自己数 `len(items)`），无影响 |
| HTTP 服务 409/400 取代 500 | 我们只用 SDK 访问 chroma，不走它的 Memory HTTP 服务，无关 |

### 6.5 若要升，落点只有两处

`requirements.txt: hy-memory==2.0.5` → `2.0.6`；`cli/helptext.py` 的 `PURPOSE` 文案里的版本号。
升级后需重打包并重跑 `probe_cli_sweep`（真实嵌入端点 + serve 的端到端）。

---

## 7 本轮回归

| 套件 | 结果 |
|---|---|
| `test_cli.py` | 84/84 |
| `test_ui.py` | 94/94（+2，连续写入的循环语义） |
| `test_queue.py` | 18/18 |
| `test_setup.py` | 103/103 |
| `test_vdb_endpoint.py` | **36/36**（原 31，+5 为 pid 复用） |
| `test_mcp.py` | 22/22（含 add → worker → search 全链路） |
| `test_oplog.py`（新增） | 25/25 |
| `test_i18n.py`（新增） | 29/29 |
| `test_encoding.py`（新增） | 10/10 |

SDK 兼容性：同套 `test_cli / test_queue / test_setup / test_mcp` 在 `hy-memory==2.0.6` 下同样全绿。
