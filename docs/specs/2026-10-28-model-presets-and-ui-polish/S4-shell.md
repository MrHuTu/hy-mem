# S4 · 交互形态：从箭头菜单改成命令式控制台（shell）

> 父级：`../00-contract.md`　｜　契约条款：C9 / C10 / C11　｜　状态：已实现

## 现象

用户原话：「ui 界面交互很多地方反人类设计，你可以仿照 pi、bash shell 等操作习惯和
界面风格来设计，别偷懒。」

旧形态一次操作要按 4~6 次键：进页面 → 读标题 → 选一项 → 作答 → 「按任意键继续」→
回主菜单 → 再选下一项。`tui.clear()` 还会把上一次结果擦掉，想对照两次检索结果只能重查。

## 设计（照 bash / 现代 agent CLI）

| 旧形态 | 新形态 |
| --- | --- |
| 一屏 11 项箭头菜单，每次操作后回到菜单 | 常驻提示符 `hy-mem ›`，敲命令，结果打在滚动区 |
| 每页结束「按任意键继续」 | **没有**——回车即回到提示符 |
| `tui.clear()` 擦掉历史 | 不清屏，上一条结果往上滚就在 |
| 菜单项与子命令两套词表 | 命令名 = 子命令名（`add` / `search` / `queue` …），别名兜住习惯（`ls` / `q` / `?`） |
| 13 项设置中心二级菜单 | `setup <项>` / `storage <子命令>` / `identity <子命令>` / `lang` |
| 未知选择只能重选 | 未知命令给 `是不是要 xxx？` 的近似建议，会话不退出 |

命令表 18 条，分 5 组（写 / 读 / 运维 / 设置 / 其他），`help` 按组列，`help <命令>` 给用法与别名。

## 关键实现

- `Shell` 类只持有 `dispatch` / 配置快照 / 历史，不碰业务逻辑；执行仍走
  `subprocess_dispatch`（子进程），后台角色改 env 只影响它自己。
- `parse_flags` 手写而非 argparse：argparse 的报错会 `sys.exit(2)`，
  在控制台里那等于把会话踢掉。
- `split_args` 手写而非 shlex：`posix=True` 会吃掉 Windows 路径里的反斜杠。
- `Shell.execute` 统一收口 `Aborted`：确认框被 Esc / Ctrl+C / EOF 取消时只取消这一次操作。

## 非 TTY（C11）

`hy-mem` 无参数在非 TTY 下仍只打简短帮助并退出（保护 agent 调用）；
要脚本化驱动控制台用 `hy-mem menu`，管道逐行喂命令，同一套命令、同一套输出。

## 验证

- `scripts/test_menu.py` 112 项：18 条命令逐个跑通、旧菜单 18 项逐一映射到命令、
  真 `run_menu` 管道循环（10 条命令后仍回到提示符、不「按任意键」、EOF 干净退出）。
- 真 CLI 实测：`printf 'help\nconfig\nqueue\nlog\nidentity\nlang en\nlang zh\nexit\n' | hy-mem menu`。
