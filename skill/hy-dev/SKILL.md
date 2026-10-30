---
name: hy-dev
description: hy-mem 项目的开发与修复流水线规范（只给开发用，不进产物、不铺到宿主）。改 cli/*.py、修用户报的 bug、准备发版打包时必读。规定三件事：① 开工先按改动类型分道（L0 文档 / L1 修复 / L2 功能 / L3 发版），避免每次都跑一遍全量；② 动手前先写「契约文档」（docs/specs/<日期>-<slug>/00-contract.md，唯一事实源），再按序号拆「分片文档」S1/R1…，每片一个 commit，完成后把状态与 commit 回写契约；③ 红测试一律先落 .local-test/，稳定后转正到 tests/test_*.py 计入 772 项验收。含十四条套件回归一条龙命令、提交纪律、打包五坑。
---

# hy-dev · 开发与修复流水线

**这个 skill 只服务开发。** `skill/hy-mem/` 是给使用者看的（会被打进产物、铺到宿主），
本目录不会被 `scripts/build_exe.py` 收集、也不会被 `hy-mem skill --install` 安装。

## 0. 什么时候读它

| 触发 | 读哪节 |
|---|---|
| 用户报 bug、贴截图、说"又坏了" | §1 分道 → §2 流水线 → §4 测试 |
| 要加命令 / 改交互 / 改配置结构 | §1 → §2 → §3 契约 |
| 准备发版、要给别人一份 zip | §1 L3 → §6 |
| 只改 `.md`、注释 | §1 L0，读完就够 |

## 1. 先分道 —— 决定"要做多少事"

**别默认全量。** 先看这次改了什么文件，再定 lane；跳过的步骤要在回复里说一句"因为只改文档，未跑测试"。

| Lane | 判定（改了什么） | 必做 | 可跳 |
|---|---|---|---|
| **L0 文档** | 只有 `*.md` / 注释 / `.gitignore` | `check_secrets` → 1 个 `docs:` commit | 全部测试、契约文档 |
| **L1 修复** | `cli/*.py` 的逻辑（不改接口） | 复现 → 契约(根因表) → 红测试 → 改 → 同路径复跑 → 全量 14 套 → 双 commit | PRD 评审 |
| **L2 功能** | 新命令 / 改交互 / 改配置结构 / 动数据结构 | 澄清 → 契约(条款) → 分片 → 红测试 → 全量 14 套 → 双 commit | — |
| **L3 发版** | 要产出 `dist/hy-mem-<ver>-win64.zip` | L1/L2 全流程 + `AGENTS.md §9` 七条 + `§10` 八步 | — |

判不准就按高一档走。**改配置结构（如"密钥只留变量名"）永远算 L2**，它必然波及多处判断逻辑。

## 2. 六阶段流水线（功能与修复共用一条）

| 阶段 | 功能开发（L2） | Bug 修复（L1） | 产物 |
|---|---|---|---|
| **0 澄清** | 关键分支用 `AskUserQuestion` 一次问清（范围/取舍/子代理模型） | **复现**：真 CLI 子进程 + 管道喂键，见 §4.3 | 决策点 / 修前日志 |
| **1 契约** | 写 `00-contract.md` §1 契约条款 + §2 分片表 | 写 `00-contract.md`，主体是**现象 + 复现命令 + 根因表** | `docs/specs/<日期>-<slug>/00-contract.md` |
| **2 切片** | 按分片表逐片实现，**每片一个 commit**（可分多次提交，但一片只对应一个 commit） | 一个根因 = 一片 | 代码 + 分片文档 |
| **3 测试** | 红测试落 `.local-test/`，绿后再转正 | 同左 | `repro_<slug>.py` / `case_<slug>.py` |
| **4 回归** | 全量 14 套 772 项 + `check_secrets` | 同左 + **同一路径修前/修后证据** | 全绿日志 |
| **5 回写** | 分片表填 `✓` + commit hash + 验证证据 | 同左 | 契约文档更新 |
| **6 提交** | `code` 与 `docs` 两个 commit，不 push | 同左 | — |

**修复为什么能共用：** 差异只有三条，全部落在"契约区写什么"上。

1. bug 的契约区主体是**根因表**，不是需求 —— 401 那次三个根因叠加，只修表症必然复发。
2. bug 必须有**同一路径的修前/修后证据**（同一串按键、同一条命令），"测试全绿"不等于用户那条路通了。
3. bug 不需要 PRD 评审 —— 用户现场就是需求；评审对象是根因表。

## 3. 契约文档与分片文档（唯一事实源 + 全流程可追溯）

### 3.1 目录与命名（标准结构）

```
docs/specs/<YYYY-MM-DD>-<slug>/
├── 00-contract.md          ← 父级：唯一事实源。不许有第二份"实时"说明
├── S1-<kebab-name>.md      ← 子级：功能分片（L2）
├── S2-<kebab-name>.md
├── R1-<kebab-name>.md      ← 子级：bug 根因分片（L1）
└── R2-<kebab-name>.md
```

- **父级固定叫 `00-contract.md`**：`00-` 前缀保证它排在最前，也便于 `ls docs/specs/*/00-contract.md` 一次列全。
- **子级序号必填**：功能用 `S<n>`（slice），修复用 `R<n>`（root cause）。序号一经分配不复用，废弃的保留文件并把状态改成 `✗ 废弃`。
- **一个 slug 一个目录**，不跨目录。跨会话接手时只看这个目录就知道做到哪。
- `docs/prd/` 里的历史文档**冻结**，不再新增；新的一律进 `docs/specs/`。

### 3.2 父子关系（每个子文件必须自我声明）

子文件头部固定三行，缺一不可：

```markdown
> 父级：../00-contract.md
> 契约条款：C2（唯一事实源，本文件只做落地，不改约定）
> 状态：执行中 / 完成
```

父文件的分片表是**唯一进度表**；子文件里不写"当前进度"，避免两处打架。

### 3.3 唯一源规则（这条最容易被违反）

| 情况 | 正确做法 | 禁止 |
|---|---|---|
| 实现时发现契约写错了 | 改 `§1`，同时在 `§3 变更记录` 记一行"改了什么/为什么/影响哪几片"，并**重走 §0 澄清** | 边写代码边悄悄改契约，代码成了事实源 |
| 分片做完 | 只回写 `§2` 的状态列与 commit 列 | 顺手改 `§1` 的文字让它"对上代码" |
| 用户中途改需求 | 新开一条变更记录，受影响分片状态退回 `☐` | 在原分片上打补丁、不留痕 |

一句话：**`§1` 只由澄清改，`§2` 只由执行改。** 代码与契约冲突时，契约是准的，代码是错的。

### 3.4 追溯链（必须能从条款追到产物）

`00-contract.md` 末尾固定一张追溯表，四列一条链：

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| C1 密钥只从 `resolve_embed_key()` 取 | R1 | `a1a7617` | 0.9.12 | `drive_setup.py … embedding "1\n"` → `✓ 1024 维` |
| C2 二次配置不追问 key | R2 | `a1a7617` | 0.9.12 | `test_setup` Q 节 3 例 |

- **commit 列**：填短 hash；未提交写 `—`。
- **产物列**：填版本号；只有源码改动、尚未重打包时写 `待打包`。
- **验收证据**：写命令 + 关键输出，别人照着能复跑。不要写"已验证"。

### 3.5 开销控制：小修不必写满

| 规模 | `00-contract.md` | 子文件 |
|---|---|---|
| 单点修复（1 个根因 / ≤1 文件） | 只填 §2 根因表（3~5 行）+ §4 追溯表 | 可省，根因表即分片表 |
| 多根因 / 跨文件 | 全填 | `R1…Rn` 每片一份 |
| 功能 | 全填，§1 是重点 | `S1…Sn` 每片一份 |

省掉子文件的，在根因表"分片"列直接写 `R1（内联）`，别留空。

## 4. 测试放哪、怎么跑（最容易搞错的一节）

### 4.1 放置规则（方案 A：先本地，再转正）

| 阶段 | 位置 | 命名 | 是否入库 |
|---|---|---|---|
| 开发期（红 → 绿） | `.local-test/` | `repro_<slug>.py`（修 bug）/ `case_<slug>.py`（功能） | 否（整目录被 `.gitignore` 忽略） |
| 稳定后**转正** | `tests/test_*.py` | 并入现有 14 套中语义最近的一套，或新增 `test_<域>.py` | 是，计入 772 |

**转正判据**：这条用例是发版必跑的门禁吗？是 → 转正；只是一次性复现 → 留在 `.local-test/`。

`.local-test/` 里的脚本必须能独立跑：

```python
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import _secrets                       # 密钥只从环境变量读，不许写明文
```

沙箱：`HY_MEM_CONFIG=<沙箱>/config.json` + `MEMORY_DATA_DIR=<沙箱>/data`，绝不指向真实库。

### 4.2 四条铁律（源自 401 事故，正文见 `AGENTS.md §8.1`）

1. **每个可配置项测两遍**：首次 + 第二次。判据类 bug 只在"再配一次"时暴露。
2. **不许把被测对象一起桩掉**：只桩网络（`netprobe.post_json`）与终端（`tui.*`）。
3. **配置断言看结构**：`apiKey` 空 + `keyEnv` 有效是常态，别只断言单字段。
4. **交互路径用真 CLI 子进程 + 管道喂键**（模板 `scripts/drive_setup.py`），不许直接调 `step_*`。

### 4.3 一条龙回归（每次 L1/L2 收尾都跑）

```bash
cd /d/tools_open/agent-bridge
for t in test_cli test_ui test_menu test_setup test_setup_ui test_vdb_endpoint test_queue test_oplog test_i18n test_encoding; do
  printf "%-20s" $t; .venv/Scripts/python.exe tests/$t.py 2>&1 | tail -1
done
# 以下三套要真实密钥：从注册表取，不落盘
.venv/Scripts/python.exe -c "
import winreg, os, runpy
k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment')
for n in ('MEMORY_EMBEDDER_API_KEY','MEMORY_LLM_API_KEY'): os.environ[n] = winreg.QueryValueEx(k, n)[0]
for t in ('test_mcp', 'test_concurrency', 'test_scene', 'test_evolve'):
    runpy.run_path(f'tests/{t}.py', run_name='__main__')
"
.venv/Scripts/python.exe scripts/check_secrets.py      # 必须 0 ERROR；WARN 逐条确认
```

期望：14 套 **772** 项全绿（cli 97 / ui 110 / menu 113 / setup 148 / setup_ui 41 / vdb 36 /
queue 21 / oplog 25 / i18n 31 / encoding 10 = 离线 632；mcp 31 / concurrency 31 / scene 43 /
evolve 35 = 真模型 140）。
数字对不上 = 有人加了用例没同步本行与 `README.md` 测试段，先对齐再继续。

## 5. 提交纪律

- **两个 commit 分开**：① 代码 + 测试（`fix:` / `feat:`）；② 文档（`docs:`，含契约与分片文档）。不要混提。
- **逐文件 `git add`**，禁止 `git add -A` / `git add .`（`data/` 之外的未跟踪目录容易误带）。
- **不 push**：push 由用户自己执行，AI 只把命令给出来。
- **不切分支**：当前在哪个分支就在哪个分支提交。
- 标题格式：`<type>(<scope>): <一句话结论>`，正文写"根因 / 改法 / 证据"，别写"修改了一些问题"。

## 6. 发版打包

**完整 SOP 只有一份，在 `AGENTS.md §9`（前置七条）+ `§10`（八步）**，这里不复制，避免两处漂移。本节只放五个致命坑：

| 坑 | 后果 |
|---|---|
| 有 hy-mem 进程占用 `dist/hy-mem` | `_stamp_old()` 改名失败 → exit 1。先 `tasklist \| grep -i hy-mem` |
| 没备份 exe 侧 `config.json` / `data/` | 构建消毒会直接删掉，用户配置丢失 |
| 先恢复 config 再验证干净 | 掩盖消毒失败。顺序必须是"验证干净 → 恢复" |
| 只看 `hy-mem.exe`，不看目录 | `_internal/` 才是运行时；对外只发 zip |
| `rm -rf dist` | 触发安全删除保护、可能被占用。用 `mv` 改名 |

前置还要确认：`cli/helptext.py: VERSION` 已改（唯一改动点）；`skill/hy-mem/SKILL.md` 已是最新（`skill/hy-dev/` 与打包无关）。

## 7. 别做

1. 别跳过复现直接改代码 —— 修完无法证明用户那条路通了。
2. 别把契约写成事后总结 —— 那只是记录，失去了约束力。
3. 别在 `docs/specs/*/00-contract.md` 之外再维护一份"当前进度"。
4. 别为了省事把新用例写进 `scripts/`（红阶段就该在 `.local-test/`）。
5. 别把明文密钥写进任何脚本、契约文档、commit message。
