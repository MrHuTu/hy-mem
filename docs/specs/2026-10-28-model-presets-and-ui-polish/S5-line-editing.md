# S5 · 行编辑：历史 / Tab 补全 / Ctrl+R 反查

> 父级：`../00-contract.md`　｜　契约条款：C12　｜　状态：已实现

## 为什么自己做，不引 prompt_toolkit

`tui.py` 已有一套跨平台按键读取（Windows `msvcrt` 扫描码 / POSIX termios）与
单行重绘（`clip_input` 保证任何输入都只占一行）。再引 prompt_toolkit 意味着
两套 stdin 抢占同一个控制台缓冲，还要给 PyInstaller 加 hiddenimports —— 风险大于收益。
缺的三件事（历史、补全、反查）在现成的按键循环里各加十行就能到位，且**冻结产物零新依赖**。

## 新增按键

| 按键 | 行为 |
| --- | --- |
| ↑ / ↓ · Ctrl+P / Ctrl+N | 翻历史；从「正在输入」上翻时先记住草稿，翻回底部还原 |
| Tab | 单候选直接填；多候选先补公共前缀，再补不动就分行列出候选 |
| Ctrl+R | 反查：输入片段即时匹配，Ctrl+R 继续往前找，回车采用，Esc/Ctrl+C 放弃并还原原行 |
| Ctrl+D | 空行 = EOF（退出控制台）；非空 = 删光标处字符（bash 语义） |
| Ctrl+K | 删到行尾 |
| Ctrl+L | 清屏并重画当前行 |

## 历史存哪

`<dataDir>/shell-history`，一行一条，最多 300 条；连续重复不重记；单条截到 200 字符。
`add` 的多行正文**不入历史**（历史是命令，不是记忆内容）。

## 两个坑

1. **EOF 与取消必须分开**：`_ask_line` 原来把 EOF 统一转成 `Aborted`，控制台会把
   「管道喂完最后一行」当成「用户放弃了这一行」，于是原地打转刷屏。
   新增 `eof_is_exit` 参数：控制台传 True（EOF = 退出），向导保持默认（EOF = 取消）。
2. **`HY_MEM_TUI=0` 不能全局设**：`test_ui` 前面的用例靠 `key_mode()` 走 `FakeReader`，
   全局关掉会让它们退化成编号菜单并卡在 stdin 上。只在控制台那一段临时设、用完还原。

## 验证

- `scripts/test_ui.py` 新增 15 项：↑ 取上一条、↑↑ 更早、↑ 后 ↓ 回草稿、
  Ctrl+P/N 等价、密码输入不翻历史、Tab 唯一候选 / 补公共前缀 / 无候选 / 补全抛异常不打断、
  Ctrl+R 采用 / 放弃、Ctrl+D 两种语义、Ctrl+K。
- `scripts/test_menu.py`：历史落盘、连续重复不重记、新会话能读到、单条长度上限。
