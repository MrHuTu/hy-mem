# Issue 草稿（提交给 hy-memory 上游）

> 标题建议：**[Bug] ultra 模式多进程部署：GraphStore 锁冲突降级机制被 client 层否决，第二个 client 进程构造必然失败**

---

## 用户影响

- **影响范围**：所有 ultra 模式下的多进程部署。同一数据目录上，第二个及以后构造 `HyMemoryClient(mode="ultra")` 的进程在构造期必然抛 `RuntimeError` 退出。
- 典型受害场景（我们生产实测）：
  - 常驻服务进程（MCP server / 嵌入式客户端）持 kuzu 锁期间，后台异步写入 worker 构造 client 失败 → 写入任务重试 3 次全部进入死信，**数据丢失**；
  - 部分任务出现"影子写入"：事实层（sqlite）落库成功，但向量/图未落 → **该条记忆永远检索不到**，且用户难以察觉；
  - CLI 短命令（stats / add 等）构造 client 即抛错，功能完全不可用。
- 错误提示建议用户改用 `mode='pro'`，但 pro 会关闭 Graph/System 2 能力——对依赖 L4/L6 图能力的用户等于功能阉割，不是可行解。

## 复现步骤

1. 准备同一数据目录（ultra 模式，chroma + kuzu 本地存储）。
2. 启动进程 A，构造 `HyMemoryClient(mode="ultra")` 并保持存活（持有 kuzu_db 文件锁）：

   ```python
   # process_a.py
   from hy_memory import HyMemoryClient
   c = HyMemoryClient(mode="ultra")
   import time; time.sleep(120)
   ```

3. 在 A 存活期间启动进程 B，同样构造 ultra client：

   ```python
   # process_b.py
   from hy_memory import HyMemoryClient
   c = HyMemoryClient(mode="ultra")   # ← 这里抛 RuntimeError
   ```

4. 观察进程 B 输出：

   ```
   RuntimeError: GraphStore (kuzu) init failed. Use mode='pro' instead of mode='ultra'
   to disable Graph, or check logs for details.
   ```

   同时 A/B 共同的日志文件中出现（证明 kuzu 层已完成降级）：

   ```
   GraphStore init failed (will degrade to no-op): IO exception: Cannot open file.
   path: ...kuzu_db - Error 5: Access is denied.. kuzu_version=0.11.3
   ```

## 版本与平台

- hy-memory：**2.0.11 / 2.0.12**（两版行为一致，已核对最新 main 分支源码）
- kuzu：0.11.3
- 平台：Windows 11 Pro（build 26200），Python 3.13（问题为文件锁语义，预期 Linux 同样受影响，仅错误码不同）
- 部署形态：单机多进程（1 常驻服务进程 + CLI 短命令 + detached 后台 worker）

## 预期与实际结果

**预期结果**：kuzu 层的降级机制生效——锁冲突的进程构造成功，GraphStore 以 no-op 模式运行（`_available=False`，所有图方法安全返回 `None`），事实/向量/检索路径不受影响。依据：

1. `graph_store_kuzu.py` `initialize()` 已捕获锁冲突并优雅降级（`_db=None, _conn=None, _available=False`）；
2. 所有运行期图方法开头均有守卫：`if not self._available or self._conn is None: return None`；
3. 降级日志原话即为设计意图：*"Degrading to no-op mode. **This is expected in multi-worker environments**"*；
4. client 层所有 graph 调用方（`_get_graph()`、`get_node`、`delete_node` 等）均已做 `None`/异常防御。

**实际结果**：`client.py` `_initialize_impl()`（2.0.11/2.0.12 的 L613-617）在 kuzu 层降级完成后检查 `_available` 并**主动 `raise RuntimeError`**，把下游已完成的降级否决为致命错误——kuzu 层精心实现的 no-op 永远不会被外界看到，第二个 ultra client 进程构造即死。

```python
# client.py _initialize_impl()，第 3 步：
self._graph_store = create_graph_store(self._config)
await self._graph_store.initialize()
if hasattr(self._graph_store, '_available') and not self._graph_store._available:
    raise RuntimeError(   # ← 与 kuzu 层的降级设计自相矛盾
        f"GraphStore ({self._config.graph_store.provider}) init failed. "
        f"Use mode='pro' instead of mode='ultra' to disable Graph, or check logs for details."
    )
```

**附带发现**：`client.py` L528-529 中 `mode == "ultra"` 分支硬编码 `enable_graph = True`，连环境变量都无法关闭，ultra 用户没有任何逃生口。

---

*报告方：hy-mem（基于 hy-memory 2.0.11 的跨会话记忆 CLI/MCP 工具）；实测 2026-11-02，可补充完整死信/影子写入现场日志。*
