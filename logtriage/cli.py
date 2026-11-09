"""命令行入口：analyze / check / watch / init / rules。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .analyze import analyze_paths, analyze_sources, build_project_index, collect_log_files, read_sources
from .config import EXAMPLE_TOML, ConfigError, FAIL_ON_CHOICES, ChannelSettings, load_settings
from .knowledge import RuleSet
from .notify import notify as send_notifications
from .render import render_json, render_markdown, render_summary, render_text
from .utils import atomic_write_text, ensure_utf8_stdout, local_now, severity_rank


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logtriage",
        description="日志分析 agent：定位报错、关联项目代码、给出修复建议并发送通知。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    analyze = subparsers.add_parser("analyze", help="分析日志并输出报告")
    _add_common(analyze)
    analyze.add_argument("logs", nargs="+", help="日志文件/目录，传 - 表示从标准输入读取")
    analyze.add_argument("-f", "--format", choices=("markdown", "text", "json"), default="markdown")
    analyze.add_argument("-o", "--output", help="把报告写入文件")
    analyze.add_argument("--notify", action="store_true", help="分析后发送通知（即使配置里 notify.enabled=false）")
    analyze.add_argument("--no-project", action="store_true", help="不索引项目代码，只根据日志本身分析")
    analyze.add_argument("--include-warnings", action="store_true", help="把 WARN 也纳入分析")
    analyze.add_argument("--fail-on", choices=FAIL_ON_CHOICES, help="达到该严重度时退出码为 1")
    analyze.add_argument("--max-groups", type=int, help="报告最多展示多少组错误")
    analyze.add_argument("-q", "--quiet", action="store_true", help="不打印报告（配合 --notify / -o 使用）")

    check = subparsers.add_parser("check", help="只看结论，用退出码表示是否有问题（适合 cron/CI）")
    _add_common(check)
    check.add_argument("logs", nargs="+", help="日志文件/目录，传 - 表示从标准输入读取")
    check.add_argument("--notify", action="store_true", help="发现达到阈值的错误时发送通知")
    check.add_argument("--no-project", action="store_true", help="不索引项目代码")
    check.add_argument("--include-warnings", action="store_true", help="把 WARN 也纳入分析")
    check.add_argument("--fail-on", choices=FAIL_ON_CHOICES, help="达到该严重度时退出码为 1")
    check.add_argument("-q", "--quiet", action="store_true", help="不打印摘要")

    watch = subparsers.add_parser("watch", help="持续盯着日志文件，出现新错误时提醒")
    _add_common(watch)
    watch.add_argument("logs", nargs="+", help="日志文件/目录")
    watch.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数，默认 2")
    watch.add_argument("--iterations", type=int, default=0, help="最多轮询多少次，0 表示一直跑")
    watch.add_argument("--notify", action="store_true", help="发现新错误时发送通知")
    watch.add_argument("--no-project", action="store_true", help="不索引项目代码")
    watch.add_argument("--include-warnings", action="store_true", help="把 WARN 也纳入分析")
    watch.add_argument("--llm", action="store_true", help="允许 watch 调用 LLM（默认关闭，避免持续烧钱）")

    init = subparsers.add_parser("init", help="生成一份配置模板")
    init.add_argument("--path", default="logtriage.toml", help="输出路径，默认 ./logtriage.toml")
    init.add_argument("--force", action="store_true", help="覆盖已存在的文件")

    rules = subparsers.add_parser("rules", help="查看已加载的错误知识库规则")
    rules.add_argument("-c", "--config", help="配置文件路径")
    rules.add_argument("--json", action="store_true", help="以 JSON 输出")

    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", help="配置文件路径（默认找 ./logtriage.toml）")
    parser.add_argument("--project", help="项目代码根目录（覆盖配置里的 project.root）")
    parser.add_argument("--no-llm", action="store_true", help="禁用 LLM，只用规则和代码索引")


def _stdin_text(logs: list[str]) -> str | None:
    if "-" in logs:
        return sys.stdin.read()
    return None


def _effective_logs(logs: list[str]) -> list[str]:
    return [item for item in logs if item != "-"]


def _force_notifications(result, settings, enabled: bool) -> list:
    if not enabled:
        return []
    settings.notify.enabled = True
    if not settings.notify.channels:
        settings.notify.channels = [ChannelSettings(kind="console", enabled=True, options={})]
    return send_notifications(result, settings.notify)


def _print_notify_results(results: list) -> None:
    for item in results:
        if item.kind in ("console",):
            continue
        status = "OK" if item.ok else "失败"
        print(f"[notify:{item.kind}] {status} {item.detail}", file=sys.stderr)


def _fail_code(result, settings, override: str | None) -> int:
    threshold = override or settings.general.fail_on
    if threshold == "none":
        return 0
    return 1 if result.count_at_least(threshold) > 0 else 0


def cmd_analyze(args, settings) -> int:
    logs = _effective_logs(args.logs)
    stdin_text = _stdin_text(args.logs)
    if not logs and stdin_text is None:
        print("请提供日志文件/目录，或用 - 从标准输入读取", file=sys.stderr)
        return 2
    result = analyze_paths(
        logs,
        settings,
        stdin_text=stdin_text,
        index_project=not args.no_project,
    )
    renderer = {"markdown": render_markdown, "text": render_text, "json": render_json}[args.format]
    report = renderer(result)
    if args.output:
        atomic_write_text(args.output, report)
        if not args.quiet:
            print(f"报告已写入 {args.output}", file=sys.stderr)
    elif not args.quiet:
        print(report)
    if args.notify:
        _print_notify_results(_force_notifications(result, settings, True))
    return _fail_code(result, settings, args.fail_on)


def cmd_check(args, settings) -> int:
    logs = _effective_logs(args.logs)
    stdin_text = _stdin_text(args.logs)
    if not logs and stdin_text is None:
        print("请提供日志文件/目录，或用 - 从标准输入读取", file=sys.stderr)
        return 2
    result = analyze_paths(
        logs,
        settings,
        stdin_text=stdin_text,
        index_project=not args.no_project,
    )
    if not args.quiet:
        print(render_summary(result))
    if args.notify:
        _print_notify_results(_force_notifications(result, settings, True))
    return _fail_code(result, settings, args.fail_on)


def cmd_watch(args, settings) -> int:
    files = collect_log_files(_effective_logs(args.logs))
    project_index = None
    if not args.no_project:
        project_index, warnings = build_project_index(settings)
        for warning in warnings:
            print(f"[warn] {warning}", file=sys.stderr)
    ruleset = RuleSet.load(settings.general.extra_rules)
    known: dict[str, int] = {}
    iteration = 0
    print(f"开始监视 {len(files)} 个日志文件，间隔 {args.interval:g}s，Ctrl+C 退出", file=sys.stderr)
    try:
        while True:
            try:
                files = collect_log_files(_effective_logs(args.logs))
                sources = read_sources(files)
            except FileNotFoundError as exc:
                print(f"[warn] {exc}", file=sys.stderr)
                sources = []
            result = analyze_sources(
                sources,
                settings,
                project_index=project_index,
                ruleset=ruleset,
                use_llm=args.llm,
            )
            current = {group.fingerprint: group.count for group in result.groups}
            new_groups = [
                group
                for group in result.groups
                if known.get(group.fingerprint) is None or group.count > known.get(group.fingerprint, 0)
            ]
            if new_groups:
                from dataclasses import replace

                stamp = local_now().strftime("%H:%M:%S")
                print(f"\n[{stamp}] 发现 {len(new_groups)} 个新增/增长的错误组", file=sys.stderr)
                print(render_text(replace(result, groups=new_groups)))
                if args.notify:
                    _print_notify_results(
                        send_notifications(
                            replace(result, groups=new_groups),
                            _with_notify_enabled(settings),
                        )
                    )
            known.update(current)
            iteration += 1
            if args.iterations and iteration >= args.iterations:
                break
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        print("\n已停止监视", file=sys.stderr)
        return 130
    return 0


def _with_notify_enabled(settings):
    settings.notify.enabled = True
    if not settings.notify.channels:
        settings.notify.channels = [ChannelSettings(kind="console", enabled=True, options={})]
    return settings.notify


def cmd_init(args) -> int:
    path = Path(args.path).expanduser()
    if path.exists() and not args.force:
        print(f"{path} 已存在；用 --force 覆盖", file=sys.stderr)
        return 2
    atomic_write_text(path, EXAMPLE_TOML)
    print(f"已生成配置文件：{path}")
    return 0


def cmd_rules(args) -> int:
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    ruleset = RuleSet.load(settings.general.extra_rules)
    for warning in ruleset.warnings:
        print(f"[warn] {warning}", file=sys.stderr)
    if args.json:
        import json

        print(json.dumps(ruleset.describe(), ensure_ascii=False, indent=2))
        return 0
    print(f"共 {len(ruleset.rules)} 条规则（内置 {ruleset.builtin_count}，自定义 {ruleset.custom_count}）")
    for item in ruleset.describe():
        print(f"- [{item['severity']}] {item['id']}: {item['title']}  ({item['source']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "init":
        return cmd_init(args)
    if args.command == "rules":
        return cmd_rules(args)

    try:
        settings = load_settings(
            getattr(args, "config", None),
            project=getattr(args, "project", None),
            no_llm=getattr(args, "no_llm", False),
        )
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if getattr(args, "include_warnings", False):
        settings.general.include_warnings = True
    if getattr(args, "max_groups", None):
        settings.general.max_groups = max(1, args.max_groups)

    try:
        if args.command == "analyze":
            return cmd_analyze(args, settings)
        if args.command == "check":
            return cmd_check(args, settings)
        if args.command == "watch":
            return cmd_watch(args, settings)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
