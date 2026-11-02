# 上游 Bug 追踪（hy-memory / Tencent-Hunyuan）

> 用途：记录上游 SDK 的已知 bug、我们下游的暂时对策、修复关联文档。上游修复后同步更新本表并退役对应下游补丁。
> 上游仓库：<https://github.com/Tencent-Hunyuan/Hy-Memory>（vendor 快照：`vendor/hy-memory/`，git clone）
> 同步纪律：**上游发新版后，先跑本表「验证方法」列，再决定是否退役下游补丁；退役后必须在「同步记录」留痕。**

## 总览

| # | 上游 Bug | 影响我们的能力 | 下游对策 | 对策落点 | 上游状态 | 我方状态 |
|---|---|---|---|---|---|---|
| U1 | GraphStore 锁冲突降级被 client 层否决 → ultra 多进程第二个 client 构造必炸 | 异步 add（worker 死信/影子写入）、CLI add/stats、多进程并发 | 方案 A：vendor 补丁接通降级（`_initialize_impl` 的 raise → warning） | `scripts/apply_upstream_patches.py`（幂等重放）+ `docs/specs/2026-11-02-ultra-graph-degrade/` | **未修**（main@3511153 仍在：client.py L613-617 raise / L528-529 ultra 硬编码开图） | **补丁已实施并验收**（2026-11-02，commit fa78cb5；red→green + 全量 14 套全绿 + 实战死信复活） |
| U2 | S2 归纳 auto 语言检测 `zh_ratio≥0.7` 对技术型中文误判为 en → 画像/模式归纳成英文 | evolve / L6 schema 语言 | 显式配置 `evolve.language=zh`（SDK 2.0.11 起原生支持，显式时换归纳提示词本体 + 11 语言 OUTPUT_LANGUAGE_DIRECTIVE 硬约束），**不用 auto** | `config.json` evolve.language=zh；运行时补丁已在 rc.9 退役 | **部分改善未根治**：2.0.12 补齐 11 语言提示词（PR #261/262）+ memhub 显式传 zh-Hans（PR #270），但 auto 检测阈值逐字未变（system2_agent.py L2336-2337，2026-11-02 实测） | 显式 zh 常态化；上游若修 auto 再评估回归 |
| U3 | client 构造期冻结 `MEMORY_VECTOR_HOST/PORT`，serve 重启换端口后长驻 client 全部 ConnectError 10061 | MCP server 长驻 client 的 add sync（search 因动态发现幸存） | 暂无代码级对策；运营层保证 serve 常驻 + 出问题重启 MCP 会话 | —（见 `docs/specs/2026-11-02-ultra-graph-degrade/issue-draft.md` 附带发现段） | **未修** | 待定对策（候选：client 连接失败自动重建） |

## U1：GraphStore 降级被否决（详细）

- **上游现象**：同数据目录第二个 ultra client 构造 `RuntimeError: GraphStore (kuzu) init failed`（kuzu_db 文件独占锁，Windows Error 5）。
- **根因**：`graph_store_kuzu.py initialize()` 捕获锁冲突已降级 no-op（`_available=False`，全部运行期方法有 `return None` 守卫，日志自称 *"expected in multi-worker environments"*），但 `client.py _initialize_impl()` 检查 `_available` 后主动 raise——降级被否决。
- **下游对策（方案 A，2026-11-02 拍板）**：vendor 补丁把 raise 改为 warning，接通 SDK 自己设计的降级。补丁以文本替换方式幂等应用（`scripts/apply_upstream_patches.py`，pip 重装 venv 后由 build_exe.py 自动重放）。
- **影响语义**：锁冲突进程的图能力 no-op（L4 关系边不写），事实/向量/检索完全正常；关系边缝隙用 `graph-build` / `vdb-reembed-graph` 回填。
- **关联文档**：`docs/specs/2026-11-02-ultra-graph-degrade/00-contract.md`（契约与追溯链）、`issue-draft.md`（上游 issue 稿）。
- **验证方法（上游新版到达时）**：
  1. `pip install hy-memory==<新版>`；
  2. 跑 `.local-test/repro_graph_lock.py`（进程 A 持锁 + 进程 B 构造）；
  3. B 构造成功且日志为 warning 降级（而非 RuntimeError）→ 上游已修 → **退役补丁**：从 `scripts/apply_upstream_patches.py` 移除对应 patch 条目、重装 SDK、复跑复现脚本确认；
  4. 更新本行状态与同步记录。
- **同步记录**：
  - 2026-11-02：发现（rc.10 打包验收实测死信/影子写入现场）；issue 稿完成待提交。
  - 2026-11-02：方案 A 补丁实施并验收（commit fa78cb5）：red（双进程 B 构造 RuntimeError）→ green（B 构造成功 + warning 降级）；test_cli 122/122（含 G2×2 补丁守护）；全量 14 套全绿；重打包覆盖安装后实战验证——先前死信 job 98/99 经 worker --once 全部落库可检索，队列清零。

## U2：S2 归纳语言 auto 误判（详细）

- **历史**：rc.5 时代 service2 归纳用户画像把中文归纳成英文；当时我们内置提示词/运行时补丁修过。
- **上游 2.0.11**：原生支持显式语言（`client.evolve(language=)` / `MemoryConfig.language` / env `MEMORY_LANGUAGE`），显式时换归纳提示词本体 + 11 语言 OUTPUT_LANGUAGE_DIRECTIVE 硬约束。我们 rc.9 退役运行时补丁改原生透传。
- **残留问题**：auto 检测（`zh_ratio >= 0.7 → zh else en`）对技术型中文（大量英文术语）误判为 en。2.0.12（= main@3511153 快照，2026-11-02 核对）该阈值逐字未变。
- **下游对策**：`config.json` 恒显式 `evolve.language=zh`；**不回归 auto**。
- **关联文档**：`docs/specs/2026-11-02-sdk-2011-language/`（rc.9 契约）。
- **验证方法（上游新版到达时）**：grep 新版 `system2_agent.py` 的 `_detect_language`/`zh_ratio`；若改为可配置阈值或更稳的语言判定 → 沙箱跑一次 auto evolve 用技术型中文样本验证 → 通过则评估是否回归 auto（仍建议显式，语义最稳）。
- **同步记录**：
  - 2026-10-30：rc.9 显式 zh 落地（退役 rc.5 运行时补丁）。
  - 2026-11-02：核对 2.0.12，auto 检测未修；用户提示"2.0.12 可能修复 service2 多语言"→ 实证为提示词补全（PR #261/262），auto 误判仍在。

## U3：向量端点构造期冻结（详细）

- **现象**：SDK `config.py` `__post_init__` 在 client 构造时读 `MEMORY_VECTOR_HOST/PORT` 冻结；serve 空闲自退后重拉换端口，长驻 client（MCP server）的 HttpClient 连旧死端口 → ConnectError 10061。search 因某层动态发现幸存，add sync 稳定失败。
- **下游对策**：暂无代码级修复；运营层：保持 serve 常驻（`serve --idle-exit 1800` 被请求续命），出现 ConnectError 时重启 MCP 会话（WorkBuddy 重连）。
- **候选方案**（待排期）：hy-mem 侧 client 连接失败自动重建（捕获 ConnectError → 重跑 `ensure_endpoint` → 换新 client；ultra 下需先 close 旧 client 释放进程内资源）。
- **关联文档**：`docs/specs/2026-11-02-ultra-graph-degrade/issue-draft.md` 附带发现段。
- **同步记录**：
  - 2026-11-02：发现于 MCP 工具实测（serve 重启窗口）。

- **下游对策（2026-11-02 落地，commit d575778，rc.11）**：
  - S1 自愈：MCP `_client()` 取缓存前 `_endpoint_stale()` 复验（env 端口心跳死 / 登记新活端口 → 放掉重建，重跑 ensure_endpoint）；CLI 短进程天然免疫。
  - S2 加固：config 新增 `servePort`（0=随机默认），serve 绑定固定端口（被占回退随机+告警）——端口不变则冻结端点恒有效。
  - 定性修正：主因是 hy-mem 侧「随机端口 + 单例不失效」两处设计；SDK 构造期读 env 属设计限制（enhancement），提 issue 时按低优先级建议附带。
  - 验收：test_vdb_endpoint 44/44（U3 四态 + S2 两态），全量 14 套全绿。
