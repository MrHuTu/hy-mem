"""argparse 组装：_Parser / build_parser / 短帮助 / help 命令。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
"""
from __future__ import annotations

from hymem.cmds.base import (  # noqa: F401
    EXIT_OK, EXIT_USAGE, _project, argparse,
    fail, json, sys,
)
from hymem.cmds import base as _base  # 全局状态经 _base 读写
from hymem.cmds.write import (  # noqa: F401
    cmd_add, cmd_update, cmd_delete, cmd_forget, cmd_finalize,
    cmd_tags, cmd_untag, cmd_archive,
)
from hymem.cmds.read import (  # noqa: F401
    cmd_search, cmd_list, cmd_get, cmd_history, cmd_profile,
    cmd_outcome,
)
from hymem.cmds.maint import (  # noqa: F401
    cmd_backup, cmd_doctor, cmd_evolve, cmd_evolve_status, cmd_graph_build,
    cmd_log, cmd_restore, cmd_stats, cmd_vdb_drop, cmd_vdb_plan,
    cmd_vdb_reembed, cmd_vdb_reembed_graph, cmd_vdb_scan,
)
from hymem.cmds.queue import (  # noqa: F401
    cmd_serve, cmd_worker, cmd_queue, cmd_retry, cmd_drain,
)
from hymem.cmds.config import (  # noqa: F401
    cmd_config, cmd_user, cmd_migrate, cmd_install, cmd_setup,
    cmd_menu, cmd_tui_test, cmd_version, cmd_skill,
)


def cmd_help(args) -> int:
    """命令说明。--json 是给智能体的入口：一次读完所有参数与语义。"""
    from hymem.ui import helptext

    spec = helptext.build_spec(build_parser())
    if args.json:
        if args.command:
            one = next((c for c in spec["commands"] if c["name"] == args.command), None)
            if one is None:
                return fail(f"没有这个命令：{args.command}（用 hy-mem help --json 看全部）", EXIT_USAGE)
            print(json.dumps(_project(one, _base._FIELDS), ensure_ascii=False))
        else:
            print(json.dumps(_project(spec, _base._FIELDS), ensure_ascii=False))
        return EXIT_OK
    if args.command and not any(c["name"] == args.command for c in spec["commands"]):
        return fail(f"没有这个命令：{args.command}", EXIT_USAGE)
    print(helptext.render_text(spec, args.command))
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    """用法错误统一 exit 1。

    argparse 默认用 2，会和「运行时错误」撞号 —— agent 拿到 2 会去查端点，
    实际只是参数写错了。这里分开：用法错 1、运行时错 2、内部错 3。
    """

    def error(self, message: str):  # noqa: D102
        self.print_usage(sys.stderr)
        print(f"hy-mem: {message}", file=sys.stderr)
        print("提示：`hy-mem help` 看全部命令，`hy-mem help --json` 拿机器可读规格", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    from hymem.cmds.entry import cmd_mcp  # 延迟导入：entry 也要 import 本模块（防环）
    from hymem.ui import helptext

    p = _Parser(
        prog="hy-mem",
        description="Hy-Memory 记忆库命令行（lite=纯向量，pro=+LLM 抽取，ultra=+Schema 归纳）",
        epilog="给智能体：`hy-mem help --json` 输出全部命令的结构化规格（参数类型/默认值/返回字段/示例）。",
    )
    p.add_argument("--json", action="store_true", help="输出机器可解析 JSON")
    p.add_argument("--quiet", action="store_true", help="成功时不打印任何内容，只看退出码")
    p.add_argument("--no-color", action="store_true", help="关闭彩色（等价 NO_COLOR=1）")
    p.add_argument("--config", metavar="PATH", help="指定配置文件路径（等价 HY_MEM_CONFIG）；放在命令行任意位置都生效")
    p.add_argument("--fields", default="", help="只保留这些顶层字段，逗号分隔（省 agent 的 token）")
    p.add_argument("--verbose", action="store_true", help="显示 SDK 日志（默认静默）")
    p.add_argument("--version", action="version", version=f"hy-mem {helptext.VERSION}", help="打印版本号并退出")
    p.add_argument("--help-json", action="store_true", help="等同于 help --json（全局规格，供 agent 读取）")
    p.add_argument("--mode", choices=["lite", "pro", "ultra"], default=None, help="覆盖 MEMORY_MODE")
    p.add_argument("--data-dir", default=None, help="覆盖 MEMORY_DATA_DIR（仅 doctor 用）")
    p.add_argument("-u", "--user", default=None, help="user_id（默认取 config.identity.userId / HY_MEM_USER_ID）")
    p.add_argument("-a", "--agent", default=None, help="agent_id（默认不设，由 SDK 用 default_agent）")
    p.add_argument("-s", "--session", default=None, help="session_id")
    sub = p.add_subparsers(dest="cmd", required=False)

    # ---- 元信息 / 交互层 ----
    ps = sub.add_parser("help", help="命令说明（--json 给智能体读的结构化规格）")
    ps.add_argument("command", nargs="?", help="只看某个命令，例如 search")
    # 与全局 --json 等价：`hy-mem help --json` 是最自然的写法，不能因为
    # argparse 的全局选项位置敏感而报错。SUPPRESS 保证不覆盖前置的全局值。
    ps.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON 规格（同全局 --json）")
    ps.set_defaults(func=cmd_help)
    sub.add_parser("menu", help="交互式主界面（等价于不带参数运行）").set_defaults(func=cmd_menu)
    ps = sub.add_parser("setup", help="交互式设置：向量模型 / 运行模式 / LLM / 沉淀模型 / 存储目录")
    ps.add_argument(
        "--step",
        default="all",
        choices=["all", "embedding", "mode", "llm", "evolve", "storage", "search", "queue"],
        help="只改其中一项（默认 all=完整四步向导）",
    )
    ps.set_defaults(func=cmd_setup)
    ps = sub.add_parser("config", help="查看 / 修改 / 重置配置")
    ps.add_argument("action", nargs="?", default="show", choices=["show", "path", "reset", "validate", "set"])
    ps.add_argument("pairs", nargs="*", help="set 时的 key=value（点路径，如 embedding.model=xxx）")
    ps.add_argument("--yes", action="store_true", help="reset 或修改嵌入模型时跳过确认")
    ps.set_defaults(func=cmd_config)

    ps = sub.add_parser("user", help="查看 / 切换 user_id（不设=通用共享，设了=独立隔离）")
    ps.add_argument("action", nargs="?", default="show", choices=["show", "set", "reset", "list"])
    ps.add_argument("uid", nargs="?", help="set 时的 user_id；reset 表示回到通用（default）")
    ps.add_argument("--reset", action="store_true", help="等价于 user reset")
    ps.set_defaults(func=cmd_user)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)
    ps = sub.add_parser("install", help="生成 PATH 上的 hy-mem 启动器（让智能体直接调用）")
    ps.add_argument("--dir", default=None, help="启动器目录，默认 %%LOCALAPPDATA%%\\hy-mem\\bin")
    ps.add_argument("--print", dest="print_only", action="store_true", help="只打印将写入的内容，不落盘")
    ps.set_defaults(func=cmd_install)
    ps = sub.add_parser("skill", help="SKILL.md 在哪、各宿主副本是否一致、要不要同步过去")
    ps.add_argument("--install", action="store_true", help="把 SKILL.md 铺到所有检测到的宿主 skill 目录")
    ps.add_argument("--dir", default=None, help="只装到指定目录（该目录下会建 hy-mem/SKILL.md）")
    ps.add_argument("--print", dest="print_only", action="store_true", help="打印 SKILL.md 全文，便于直接读")
    ps.set_defaults(func=cmd_skill)
    sub.add_parser("tui-test", help="按键自检：在真实终端里验证方向键/鼠标解码").set_defaults(func=cmd_tui_test)
    sub.add_parser("version", help="版本与安装信息（--json 供自动升级比对）").set_defaults(func=cmd_version)
    sub.add_parser("migrate", help="把旧安装的数据目录迁入当前安装根（升级接管后的收尾）").set_defaults(func=cmd_migrate)

    ps = sub.add_parser("add", help="写一条记忆")
    ps.add_argument("text", nargs="?", help="记忆正文；省略则从 stdin 读")
    ps.add_argument("--source", default="cli", help="来源标记")
    ps.add_argument("--split", action="store_true",
                    help="仅兜底：按行拆成多条独立写入（优先自行分条、分多次 add）；"
                         "单次最多 50 条，超出截断并告警；"
                         "--scene 下不生效（整段轨迹按一条写入，回执标 split_ignored）")
    ps.add_argument("--tag", action="append", default=None, help="绑定标签，可重复：--tag work --tag apollo")
    ps.add_argument("--scene", choices=["working", "coding"], default=None,
                    help="生产力场景（coding=编码/调试/SQL；working=文档/研究/操作）。需配 --trace 工具轨迹；"
                         "此路径按整段轨迹写入，--split 不生效")
    ps.add_argument("--trace", default=None,
                    help="messages JSON 文件路径（含 tool_calls/role=tool 的 OpenAI 数组，- 为 stdin），--scene 时必填")
    ps.add_argument("--workspace", default=None,
                    help="按仓库/项目隔离（建议 git remote URL），--scene 时可选")
    ps.add_argument("--branch", default=None, help="分支名（需同时传 --workspace）")
    ps.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help="只入队立即返回，由 worker 异步消费（pro 单条落库可达约 2 分钟，批量/拆条更久）",
    )
    ps.add_argument("--metadata", default=None, help="附加元数据，JSON 对象：--metadata '{\"project\":\"iisw\"}'")
    ps.add_argument("--memory-at", dest="memory_at", default=None, help="记忆发生时间（补录历史用）：2026-10-27 或 2026-10-27 14:30:00")
    ps.add_argument("--request-id", dest="request_id", default=None, help="幂等写 id：同一 id 重复提交只落一条")
    ps.set_defaults(func=cmd_add)

    ps = sub.add_parser("search", help="语义检索")
    ps.add_argument("query", nargs="?", default=None, help="查询语句；越接近原话命中率越高（或用 --queries 传上下文变体）")
    ps.add_argument("-n", "--limit", type=int, default=10, help="返回条数")
    ps.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="相似度下限，默认取 HY_MEM_MIN_SCORE 或 0.35",
    )
    ps.add_argument("--tag", action="append", default=None, help="按标签过滤（自动多取候选再筛）")
    ps.add_argument("--tag-mode", choices=["all", "any"], default="all", help="多个标签是 AND 还是 OR")
    ps.add_argument("--since", default=None, help="只要这段时间之后：30m/12h/7d/30d/1y 或 2026-01-01")
    ps.add_argument("--until", default=None, help="只要这段时间之前，格式同 --since")
    ps.add_argument("--scene", choices=["working", "coding"], default=None,
                    help="生产力场景检索（facts/experiences 双路返回）；缺省 chat；不支持 --queries")
    ps.add_argument("--workspace", default=None, help="按仓库/项目过滤（--scene 时可选）")
    ps.add_argument("--branch", default=None, help="分支过滤（需同时传 --workspace）")
    ps.add_argument("--chain-block", action="store_true",
                    help="（coding）注入版本演进链块（本次调用临时开启）")
    ps.add_argument("--source-filter", default=None, help="按来源白名单过滤（如 mcp）")
    ps.add_argument(
        "--schema", dest="schema", action="store_true", default=True,
        help="（ultra）附带 L6 行为模式：按语义检索 schema，独立成段不占 -n",
    )
    ps.add_argument(
        "--no-schema", dest="schema", action="store_false",
        help="只要普通记忆，不要 L6 行为模式",
    )
    ps.add_argument(
        "--schema-limit", type=int, default=0,
        help="L6 返回条数，默认取 config.evolve.schemaLimit（3）",
    )
    ps.add_argument(
        "--queries", default=None,
        help=(
            "逗号分隔 ≤5 条查询：只有最后一条是检索目标，前面的仅用于上下文消解"
            "（SDK 不并行召回/不融合；与位置参数 query 二选一）"
        ),
    )
    ps.add_argument(
        "--search-mode", choices=["default", "agentic"], default="default",
        help="agentic=智能体重写扩展检索（更慢更贵，命中率上限更高）",
    )
    ps.add_argument(
        "--no-rerank", dest="rerank", action="store_false", default=True,
        help="关闭 SDK 侧重排（省时省 token，排序质量下降）",
    )
    ps.set_defaults(func=cmd_search)

    ps = sub.add_parser("outcome", help="按 request_id 查写入结果（含异步/scene 写入）")
    ps.add_argument("request_id", help="add 返回的 request_id")
    ps.set_defaults(func=cmd_outcome)

    ps = sub.add_parser("profile", help="查看 L0 用户画像（基础信息，如姓名/职业）")
    ps.add_argument("--full", action="store_true",
                    help="返回 SDK 组装好的完整画像（get_user_profile，ultra 附 balanced catalog）")
    ps.set_defaults(func=cmd_profile)

    ps = sub.add_parser(
        "evolve",
        help="L6 认知归纳（ultra 专属）：把全量事实归纳成行为模式",
    )
    ps.add_argument(
        "--model",
        default="",
        help="本次使用的归纳模型（不改配置）；不填用 evolve.model，再退到 llm.model",
    )
    ps.add_argument("--batches", type=int, default=None, help="最多跑几批（调试用）")
    ps.add_argument(
        "--language",
        default="",
        help="本次归纳输出语言 zh/en（不改配置）；不填用 evolve.language（auto 跟随 SDK 检测）",
    )
    ps.set_defaults(func=cmd_evolve)

    ps = sub.add_parser(
        "evolve-status",
        help="查看 L6 自动沉淀状态与历史（观察窗口，所有模式可看）",
    )
    ps.add_argument("-n", "--limit", type=int, default=20,
                    help="返回最近几条记录（默认 20，上限 100）")
    ps.set_defaults(func=cmd_evolve_status)

    ps = sub.add_parser("list", help="列出记忆")
    ps.add_argument("-n", "--limit", type=int, default=20)
    ps.add_argument("--offset", type=int, default=0)
    ps.add_argument("--order", choices=["desc", "asc"], default="desc", help="desc 最新在前（默认）/ asc 最旧在前")
    ps.add_argument("--tag", action="append", default=None, help="按标签过滤")
    ps.add_argument("--tag-mode", choices=["all", "any"], default="all")
    ps.set_defaults(func=cmd_list)

    ps = sub.add_parser("get", help="按 id 读")
    ps.add_argument("memory_id", help="记忆 id（list 命令里能看到）")
    ps.set_defaults(func=cmd_get)

    ps = sub.add_parser("delete", help="按 id 删")
    ps.add_argument("memory_id", help="记忆 id；不可恢复")
    ps.add_argument("--dry-run", action="store_true", help="只确认这条记忆存在并打印它，不真删")
    ps.set_defaults(func=cmd_delete)

    ps = sub.add_parser("update", help="改一条记忆的内容（旧版本保留在演化链）")
    ps.add_argument("memory_id", help="记忆 id（list 命令里能看到）")
    ps.add_argument("content", nargs="?", help="新内容；省略则从 stdin 读")
    ps.set_defaults(func=cmd_update)

    ps = sub.add_parser("history", help="查一条记忆的变更历史（演化链版本）")
    ps.add_argument("memory_id", help="记忆 id")
    ps.set_defaults(func=cmd_history)

    ps = sub.add_parser("forget", help="按范围批量删（需 --yes）")
    ps.add_argument("--yes", action="store_true", help="确认执行；不加只打印影响范围")
    ps.add_argument("--dry-run", action="store_true", help="只统计会删掉多少条，不做任何改动")
    ps.set_defaults(func=cmd_forget)

    ps = sub.add_parser("finalize", help="收尾 session（触发演化）")
    ps.add_argument("--language", default=None,
                    help="episode 叙事输出语言（如 zh）；缺省取 config.evolve.language，再缺省 SDK 自动")
    ps.set_defaults(func=cmd_finalize)

    ps = sub.add_parser("stats", help="运行指标 / 库体积健康度")
    ps.add_argument("--minutes", type=int, default=5)
    ps.add_argument("--health", action="store_true", help="库体积健康度：条数/体积/延迟预期/归档建议")
    ps.add_argument("--vacuum", action="store_true", help="配合 --health：清理已完成 job 记录并 VACUUM")
    ps.add_argument("--max-scan", type=int, default=5000, help="--health 时最多扫描多少条记忆")
    ps.set_defaults(func=cmd_stats)

    # ---- 异步队列 ----
    ps = sub.add_parser("serve", help="向量库服务（数据唯一所有者；长驻单例，空闲自退）")
    ps.add_argument("--idle-exit", type=float, default=1800.0,
                    help="空闲超过 N 秒后退出（0=常驻；默认 1800）")
    ps.add_argument("--timeout", type=float, default=0, help="总运行时长上限秒（0=不限）")
    ps.set_defaults(func=cmd_serve)

    ps = sub.add_parser("worker", help="消费异步任务（单进程 supervisor + 多消费线程）")
    ps.add_argument("--once", action="store_true", help="只消费当前队列，清空即退出（延迟重试任务留待下次）")
    ps.add_argument("--max", type=int, default=0, help="最多处理 N 个任务后退出（0=不限）")
    ps.add_argument("--poll", type=float, default=1.0, help="空闲轮询间隔秒")
    ps.add_argument("--idle-exit", type=float, default=0, help="空闲超过 N 秒后退出（0=常驻）")
    ps.add_argument("--timeout", type=float, default=0, help="总运行时长上限秒（0=不限）")
    ps.add_argument("--lanes", type=int, default=0, help="消费线程数（0=取 HY_MEM_MAX_WORKERS，默认 3）")
    ps.add_argument("--no-recover", action="store_true", help="不回收僵死的 running 任务")
    ps.add_argument("--no-fast-exit", action="store_true",
                    help="禁用「30s 没抢到任务且队列空」的早退（端点拉起的 server-host worker 用）")
    ps.set_defaults(func=cmd_worker)

    ps = sub.add_parser("queue", help="查看队列")
    ps.add_argument("--status", choices=["pending", "running", "done", "dead"], default=None)
    ps.add_argument("-n", "--limit", type=int, default=20)
    ps.add_argument("--recover", action="store_true", help="先把僵死任务放回待处理")
    ps.set_defaults(func=cmd_queue)

    ps = sub.add_parser("mcp", help="MCP 服务器（默认 stdio；--transport http 起 HTTP 服务）")
    ps.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ps.add_argument("--port", type=int, default=18088, help="HTTP 模式监听端口（默认 18088）")
    ps.set_defaults(func=cmd_mcp)

    ps = sub.add_parser("drain", help="阻塞等待队列清空")
    ps.add_argument("--timeout", type=float, default=300)
    ps.add_argument("--poll", type=float, default=1.0)
    ps.set_defaults(func=cmd_drain)

    ps = sub.add_parser("retry", help="重投死信任务（job_id / dead / --shadow）")
    ps.add_argument("job_id", nargs="?", default="", help="任务 id；填 dead 表示全部死信；留空配合 --shadow")
    ps.add_argument("--all-dead", action="store_true", help="等价于 job_id=dead")
    ps.add_argument("--shadow", action="store_true",
                    help="重投「只落了影子节点、检索不到」的完成任务（PRD F9 自愈）")
    ps.set_defaults(func=cmd_retry)

    # ---- 标签 ----
    ps = sub.add_parser("tags", help="标签清单 / 从已有记忆同步自动标签")
    ps.add_argument("--min-count", type=int, default=1)
    ps.add_argument("--refresh", action="store_true", help="从 SDK 拉全量补齐 LLM 自动标签并清理孤儿")
    ps.add_argument("--max-refresh", type=int, default=2000)
    ps.set_defaults(func=cmd_tags)

    ps = sub.add_parser("untag", help="解除标签绑定")
    ps.add_argument("memory_id", nargs="?")
    ps.add_argument("--tag", default=None, help="只解除某个标签（不配 memory_id 时全局生效，需 --yes）")
    ps.add_argument("--yes", action="store_true")
    ps.set_defaults(func=cmd_untag)

    ps = sub.add_parser("archive", help="按标签或记忆层归档统计")
    ps.add_argument("--by", choices=["tag", "layer"], default="tag")
    ps.add_argument("--min-count", type=int, default=1)
    ps.add_argument("--max-scan", type=int, default=2000, help="--by layer 时最多扫描条数")
    ps.set_defaults(func=cmd_archive)

    ps = sub.add_parser("log", help="最近的操作日志（默认 50 条）")
    ps.add_argument("-n", "--limit", type=int, default=50, help="返回最近 N 条（最多保留 50 条）")
    ps.add_argument("--clear", action="store_true", help="清空日志（不可恢复）")
    ps.set_defaults(func=cmd_log)

    # ---- S4/S5/S6（docs/specs/2026-11-02-sdk-alignment-gaps/）：迁移与运维 ----
    ps = sub.add_parser("vdb-scan", help="扫描向量空间（只读）")
    ps.add_argument("--root", default=None, help="向量空间根目录（缺省=数据目录）")
    ps.add_argument("--whole", action="store_true", help="不按 user 拆分，报全库汇总")
    ps.set_defaults(func=cmd_vdb_scan)

    ps = sub.add_parser("vdb-plan", help="reembed 差量计划（只读）")
    ps.add_argument("--source", required=True, help="源向量空间目录")
    ps.add_argument("--target", required=True, help="目标向量空间目录（新维度/新模型）")
    ps.add_argument("--graph-source", default=None, help="源 Kuzu 图路径（附带图差量）")
    ps.set_defaults(func=cmd_vdb_plan)

    ps = sub.add_parser("vdb-reembed", help="全量重嵌入 source→target（skip_existing 断点续跑）")
    ps.add_argument("--source", required=True, help="源向量空间目录")
    ps.add_argument("--target", required=True, help="目标向量空间目录")
    ps.add_argument("--batch-size", type=int, default=64)
    ps.add_argument("--max-inflight", type=int, default=2)
    ps.add_argument("--roles", default=None, help="只重嵌这些 role（逗号分隔）")
    ps.add_argument("--yes", action="store_true", help="确认执行（不加只报错）")
    ps.set_defaults(func=cmd_vdb_reembed)

    ps = sub.add_parser("vdb-reembed-graph", help="Kuzu 图向量重建（新维度）")
    ps.add_argument("--source", required=True, help="源图路径")
    ps.add_argument("--target", required=True, help="目标图路径")
    ps.add_argument("--dims", type=int, required=True, help="目标向量维度")
    ps.add_argument("--batch-size", type=int, default=64)
    ps.add_argument("--yes", action="store_true", help="确认执行")
    ps.set_defaults(func=cmd_vdb_reembed_graph)

    ps = sub.add_parser("vdb-drop", help="删除向量空间（破坏性）")
    ps.add_argument("--path", required=True, help="要删除的空间目录")
    ps.add_argument("--roles", default=None, help="只删这些 role（逗号分隔）")
    ps.add_argument("--yes", action="store_true", help="确认执行")
    ps.set_defaults(func=cmd_vdb_drop)

    ps = sub.add_parser("backup", help="数据面快照（需数据面静默）")
    ps.add_argument("--to", required=True, help="快照输出目录")
    ps.set_defaults(func=cmd_backup)

    ps = sub.add_parser("restore", help="从快照恢复数据面（SDK 自带回滚；破坏性）")
    ps.add_argument("--snapshot", required=True, help="快照目录（backup --to 的输出）")
    ps.add_argument("--target", default=None, help="恢复目标（缺省=当前数据目录）")
    ps.add_argument("--yes", action="store_true", help="确认执行")
    ps.set_defaults(func=cmd_restore)

    ps = sub.add_parser("graph-build", help="对存量记忆回填关系图（真模型，耗时长）")
    ps.add_argument("--limit", type=int, default=10000, help="最多处理多少条记忆")
    ps.add_argument("--clear-first", action="store_true", help="构建前清空旧图")
    ps.add_argument("--entities", action="store_true", help="附带重建实体库")
    ps.add_argument("--rebuild-entities", dest="rebuild_entities", action="store_true",
                    help="实体库全量重建（缺省增量）")
    ps.set_defaults(func=cmd_graph_build)

    return p


def _print_short_help() -> None:
    from hymem.ui import helptext

    print(helptext.render_text(helptext.build_spec(build_parser())))
    print()
    print("非交互环境：请直接调用子命令；机器可读规格用 `hy-mem help --json`。", file=sys.stderr)
