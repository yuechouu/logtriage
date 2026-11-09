"""报告渲染：Markdown / 纯文本 / JSON。"""

from __future__ import annotations

from .models import AnalysisResult, ErrorGroup
from .utils import isoformat, json_dumps, truncate

_SEVERITY_LABEL = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
}


def _short_sources(result: AnalysisResult, limit: int = 6) -> str:
    if not result.sources:
        return "（无）"
    names = [source.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for source in result.sources]
    text = "、".join(names[:limit])
    if len(names) > limit:
        text += f" 等 {len(names)} 个"
    return text


def _analyzer_label(value: str) -> str:
    return {
        "heuristic": "启发式",
        "heuristic+code": "启发式 + 代码索引",
        "rules": "错误知识库",
        "hybrid": "规则 + 代码索引",
        "llm": "LLM",
    }.get(value, value or "启发式")


def render_markdown(result: AnalysisResult) -> str:
    lines: list[str] = []
    lines.append("# 日志分析报告")
    lines.append("")
    lines.append(f"- 生成时间：{isoformat(result.generated_at) or '—'}")
    lines.append(f"- 输入：{_short_sources(result)}")
    lines.append(f"- 总行数：{result.total_lines}，事件：{result.total_events}，错误事件：{result.error_events}")
    if result.first_ts or result.last_ts:
        lines.append(f"- 时间范围：{isoformat(result.first_ts) or '?'} ~ {isoformat(result.last_ts) or '?'}")
    if result.project:
        lines.append(f"- 代码索引：`{result.project}`")
    if result.level_counts:
        counts = "、".join(f"{level} {count}" for level, count in sorted(result.level_counts.items()))
        lines.append(f"- 等级分布：{counts}")
    if result.services:
        top = sorted(result.services.items(), key=lambda item: -item[1])[:8]
        lines.append("- 服务：" + "、".join(f"{name}（{count}）" for name, count in top))
    lines.append(f"- 错误组：{len(result.groups)}，最高严重度：{_SEVERITY_LABEL.get(result.top_severity, result.top_severity)}")
    if result.notes:
        lines.append("")
        lines.append("## 备注")
        for note in result.notes:
            lines.append(f"- {note}")
    lines.append("")

    if not result.groups:
        lines.append("没有发现需要处理的错误。")
        lines.append("")
        return "\n".join(lines)

    lines.append("## 概览")
    lines.append("")
    lines.append("| # | 严重度 | 次数 | 标题 |")
    lines.append("|---|---|---|---|")
    for index, group in enumerate(result.groups, 1):
        title = group.title.replace("|", "\\|")
        lines.append(
            f"| {index} | {_SEVERITY_LABEL.get(group.severity, group.severity)} | {group.count} | {title} |"
        )
    lines.append("")

    for index, group in enumerate(result.groups, 1):
        lines.extend(_render_group_markdown(index, group))
    return "\n".join(lines)


def _render_group_markdown(index: int, group: ErrorGroup) -> list[str]:
    lines: list[str] = []
    lines.append(f"## {index}. [{_SEVERITY_LABEL.get(group.severity, group.severity)}] {group.title}")
    lines.append("")
    lines.append(f"- 次数：{group.count}")
    lines.append(f"- 首次：{isoformat(group.first_seen) or '—'}")
    lines.append(f"- 最近：{isoformat(group.last_seen) or '—'}")
    lines.append(f"- 指纹：`{group.fingerprint}`")
    if group.rule_title:
        lines.append(f"- 命中规则：{group.rule_title}" + (f"（`{group.rule_id}`）" if group.rule_id else ""))
    lines.append(f"- 置信度：{group.confidence:.0%}（{_analyzer_label(group.analyzer)}）")
    lines.append("")
    if group.root_cause:
        lines.append(f"**可能原因**：{group.root_cause}")
        lines.append("")
    if group.code_analysis:
        lines.append(f"**代码分析**：{group.code_analysis}")
        lines.append("")

    if group.code_refs:
        lines.append("**相关代码**：")
        lines.append("")
        for ref in group.code_refs:
            lines.append(f"- `{ref.label()}` — {ref.note}")
            if ref.snippet:
                lines.append("")
                lines.append("```")
                lines.extend(ref.snippet)
                lines.append("```")
        lines.append("")

    if group.fixes:
        lines.append("**修复建议**：")
        for fix in group.fixes:
            lines.append(f"- {fix}")
        lines.append("")

    if group.evidence:
        lines.append("<details><summary>日志样例</summary>")
        lines.append("")
        lines.append("```")
        for event in group.evidence:
            location = f"{event.source}:{event.line_no}" if event.source else f"line {event.line_no}"
            lines.append(f"[{location}] {truncate(event.raw, 500)}")
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def render_text(result: AnalysisResult) -> str:
    lines: list[str] = []
    if result.generated_at:
        lines.append(f"日志分析 @ {isoformat(result.generated_at)}")
    lines.append(
        f"输入：{_short_sources(result)} | 事件 {result.total_events} | 错误事件 {result.error_events} | "
        f"错误组 {len(result.groups)} | 最高 {result.top_severity}"
    )
    if result.project:
        lines.append(f"代码索引：{result.project}")
    if result.notes:
        lines.append("备注：")
        for note in result.notes:
            lines.append(f"  - {note}")
    lines.append("")

    if not result.groups:
        lines.append("没有发现需要处理的错误。")
        return "\n".join(lines)

    for index, group in enumerate(result.groups, 1):
        lines.append(f"{index}. [{group.severity.upper()}] {group.title}")
        lines.append(f"   次数 {group.count} | 最近 {isoformat(group.last_seen) or '—'} | 指纹 {group.fingerprint}")
        if group.rule_title:
            lines.append(f"   规则：{group.rule_title}")
        if group.root_cause:
            lines.append(f"   原因：{group.root_cause}")
        if group.code_analysis:
            lines.append(f"   分析：{group.code_analysis}")
        for ref in group.code_refs[:4]:
            lines.append(f"   代码：{ref.label()} ({ref.note})")
        for fix in group.fixes[:6]:
            lines.append(f"   修复：{fix}")
        lines.append("")
    return "\n".join(lines)


def render_summary(result: AnalysisResult, limit: int = 5) -> str:
    if not result.groups:
        return f"logtriage: 未发现错误（事件 {result.total_events} 条）"
    parts = [f"logtriage: {len(result.groups)} 个错误组，最高 {result.top_severity}"]
    for group in result.groups[:limit]:
        parts.append(f"[{group.severity}] {truncate(group.title, 80)} ×{group.count}")
    return "\n".join(parts)


def render_json(result: AnalysisResult) -> str:
    return json_dumps(result)
