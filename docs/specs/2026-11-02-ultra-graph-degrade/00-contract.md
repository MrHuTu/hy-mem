# 契约：ultra GraphStore 降级续命（上游 bug 规避补丁 · 方案 A）

- 父级：无（独立 bug 修复）
- 日期：2026-11-02
- 状态：**实施中**
- 上游追踪：`docs/UPSTREAM-ISSUES.md` U1；issue 稿 `issue-draft.md`（用户手动提交）
- 级别：轻需求 + 纯后端（red→green closure + 测试守护，不走 grill 全链路）

## 1. 问题（一句话）

SDK 2.0.11/2.0.12 ultra 模式下 kuzu GraphStore 独占锁冲突时，kuzu 层已优雅降级
no-op（`_available=False`），但 `client.py _initialize_impl()` 又主动 raise
RuntimeError，导致第二个及以后的 ultra client 进程构造全灭——hy-mem 多进程
架构（MCP server / CLI / detached worker / serve）下表现为异步写死信、影子写入、
CLI stats/add 直接抛错。

## 2. 修复方案（方案 A，用户 2026-11-02 拍板）

**接通 SDK 自己设计好的降级**：把 `_initialize_impl()` 中 GraphStore init 失败的
`raise RuntimeError`（2.0.12 L613-617）改为 `logger.warning` 降级续命。

- 补丁落点 ×2：
  - `.venv/Lib/site-packages/hy_memory/client.py`（PyInstaller 打包实际来源）
  - `vendor/hy-memory/hy_memory/client.py`（仓库留档；vendor main@3511153 与
    PyPI 2.0.12 内容一致，仅行尾符差异，同一文本补丁两处通用）
- **幂等重放**：`scripts/apply_upstream_patches.py` 以精确文本替换应用补丁，
  带标记 `[hy-mem patch 2026-11-02]`；已打过则跳过。`scripts/build_exe.py`
  打包开头调用它，保证 pip 重装 venv 后补丁不丢。
- requirements.txt：hy-memory==2.0.11 → **2.0.12**（2026-11-02 已 pip 升级，
  vendor 同步 66 commit 至 main@3511153）。

## 3. 补丁语义与影响面

| 路径 | 补丁前 | 补丁后 |
|---|---|---|
| 异步 add（worker 撞锁） | client 构造炸 → 死信/影子写入 | 构造成功，事实+向量照写（图边 no-op） |
| 同步并发 add | WriteLock 串行化，构造期可能炸 | 行为不变且构造不再炸 |
| search / list / get | 撞锁进程构造即死 | 正常（图查询走 None 防御路径） |
| evolve / 图查询（非持锁进程） | 不可用（进程死） | 图能力 no-op（上游调用方均有 None 防御） |
| 关系边缝隙 | — | `graph-build` / `vdb-reembed-graph` 回填 |

安全依据：kuzu 层所有运行期方法开头 `if not self._available or self._conn is
None: return None`；client 层 `_get_graph()`/`get_node`/`delete_node` 等调用方
均有 None/异常防御；上游日志原话 *"expected in multi-worker environments"*。

## 4. 追溯链

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| §2 补丁本体 | S1 | （待填） | venv+vendor client.py 降级 | repro_graph_lock.py B 进程构造成功（green） |
| §2 幂等重放 | S2 | （待填） | scripts/apply_upstream_patches.py + build_exe.py 挂钩 | 脚本幂等（二次运行跳过）；build 输出含补丁确认 |
| §3 守护 | S3 | （待填） | tests/test_cli.py 补丁标记守护断言 | test_cli 全绿含新断言 |
| §1 red 复现 | R1 | （待填，落 .local-test） | repro_graph_lock.py 补丁前 B 进程 RuntimeError | red 证据输出 |

## 5. 验收口径

- red：补丁前 `.local-test/repro_graph_lock.py` 复现 B 进程 RuntimeError（留输出）。
- green：补丁后同脚本 B 进程构造成功、日志出现降级 warning、`add`/`search` 正常。
- 守护：test_cli 新增断言「site-packages client.py 含补丁标记」防 pip 重装丢失。
- 全量：14 套测试套件全绿（不低于 842 项基线 + 新增）。
- 发版：重新打包 → 解压覆盖 `D:\tools_open\hy-mem` → exe 端 doctor/search 冒烟。

## 6. 变更记录

- 2026-11-02：立项。vendor 拉至 main@3511153（66 commit），venv 升 2.0.12，
  确认上游未修（raise 仍在 L616 / ultra 硬编码 L529）；U2 附带核对：2.0.12
  多语言=提示词补全，auto 阈值未变。
