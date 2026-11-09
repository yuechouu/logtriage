"""分析编排：读日志 → 解析 → 识别 → 聚合 → 规则/代码/LLM 增强。"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import Settings
from .detect import classify_event
from .knowledge import RuleSet
from .llm import LLMClient, LLMError
from .models import AnalysisResult, ErrorGroup, LogEvent
from .parse import parse_text
from .project import ProjectIndex
from .signature import annotate_event_times, group_events
from .utils import decode_bytes_safe, local_now, severity_rank, short_type_name

LOG_SUFFIXES = {".log", ".txt", ".json", ".jsonl", ".ndjson", ".out", ".err", ".trace"}
_PROJECT_MARKERS = (
    "pyproject.toml",
    "package.json",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Cargo.toml",
    "composer.json",
    ".git",
)


def is_log_file(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".log") or ".log." in name:
        return True
    return path.suffix.lower() in LOG_SUFFIXES


def collect_log_files(paths: list[str], max_files: int = 2000) -> list[Path]:
    """文件直接用；目录则递归收集常见日志后缀。"""
    files: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"日志路径不存在：{path}")
        if path.is_dir():
            matched = sorted(item for item in path.rglob("*") if item.is_file() and is_log_file(item))
            if not matched:
                # 目录里没有 .log 文件时，退而取所有小文本文件，避免用户空手而归
                matched = sorted(item for item in path.rglob("*") if item.is_file())
            for item in matched:
                key = str(item.resolve())
                if key not in seen:
                    seen.add(key)
                    files.append(item)
                if len(files) >= max_files:
                    return files
        else:
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                files.append(path)
    return files


def read_sources(paths: list[Path], stdin_text: str | None = None) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError as exc:
            sources.append((str(path), f"[logtriage] 读取失败：{exc}"))
            continue
        sources.append((str(path), decode_bytes_safe(data)))
    if stdin_text is not None:
        sources.append(("<stdin>", stdin_text))
    return sources


def detect_project_root(explicit: str | None = None) -> tuple[Path | None, list[str]]:
    warnings: list[str] = []
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir():
            return path.resolve(), warnings
        warnings.append(f"项目目录不存在，跳过代码索引：{path}")
        return None, warnings
    current = Path.cwd()
    for parent in (current, *current.parents):
        if any((parent / marker).exists() for marker in _PROJECT_MARKERS):
            return parent.resolve(), warnings
    return None, warnings


def build_project_index(
    settings: Settings,
    explicit_root: str | None = None,
) -> tuple[ProjectIndex | None, list[str]]:
    root, warnings = detect_project_root(explicit_root or settings.project.root or None)
    if root is None:
        return None, warnings
    index = ProjectIndex(
        root,
        excludes=settings.project.excludes or None,
        max_file_kb=settings.project.max_file_kb,
        max_files=settings.project.max_files,
    )
    index.build()
    warnings.extend(index.warnings)
    return index, warnings


def _collect_stats(events: list[LogEvent]) -> tuple[dict[str, int], dict[str, int]]:
    level_counts: dict[str, int] = {}
    services: dict[str, int] = {}
    for event in events:
        level = (event.level or "UNKNOWN").upper()
        level_counts[level] = level_counts.get(level, 0) + 1
        if event.service:
            services[event.service] = services.get(event.service, 0) + 1
    return level_counts, services


def _apply_llm(
    groups: list[ErrorGroup],
    settings: Settings,
    client: LLMClient,
    result: AnalysisResult,
) -> bool:
    if not groups or settings.llm.max_groups <= 0:
        return False
    candidates = sorted(
        groups,
        key=lambda group: (-severity_rank(group.severity), -group.count),
    )[: settings.llm.max_groups]
    used = False
    for group in candidates:
        try:
            analysis = client.analyze_group(group)
        except LLMError as exc:
            result.notes.append(f"LLM 分析中止：{exc}")
            break
        if analysis.get("root_cause"):
            group.root_cause = analysis["root_cause"]
        if analysis.get("analysis"):
            group.code_analysis = analysis["analysis"]
        if analysis.get("fixes"):
            group.fixes = list(dict.fromkeys(analysis["fixes"] + group.fixes))[:10]
        if analysis.get("severity"):
            group.severity = analysis["severity"]
        if analysis.get("confidence"):
            group.confidence = max(group.confidence, analysis["confidence"])
        if group.code_refs:
            group.analyzer = "hybrid"
        else:
            group.analyzer = "llm"
        used = True
    return used


def _sort_groups(groups: list[ErrorGroup]) -> None:
    groups.sort(
        key=lambda group: (
            -severity_rank(group.severity),
            -group.count,
            -(group.last_seen.timestamp() if group.last_seen else 0.0),
            group.title,
        )
    )


def _subset_result(result: AnalysisResult, groups: list[ErrorGroup]) -> AnalysisResult:
    from dataclasses import replace

    return replace(result, groups=groups)


def analyze_sources(
    sources: list[tuple[str, str]],
    settings: Settings,
    *,
    project_index: ProjectIndex | None = None,
    ruleset: RuleSet | None = None,
    use_llm: bool | None = None,
    llm_client: LLMClient | None = None,
) -> AnalysisResult:
    result = AnalysisResult(
        sources=[name for name, _ in sources],
        generated_at=local_now(),
    )
    all_events: list[LogEvent] = []
    for name, text in sources:
        result.total_lines += len(text.splitlines())
        events = parse_text(text, source=name)
        for event in events:
            classify_event(event, settings.general.include_warnings)
        all_events.extend(events)

    result.total_events = len(all_events)
    result.level_counts, result.services = _collect_stats(all_events)
    result.first_ts, result.last_ts = annotate_event_times(all_events)

    selected = [
        event
        for event in all_events
        if event.is_error or (settings.general.include_warnings and event.is_warning)
    ]
    result.error_events = len(selected)
    groups = group_events(selected)

    if ruleset is None:
        ruleset = RuleSet.load(settings.general.extra_rules)
    for group in groups:
        ruleset.apply(group)
    result.notes.extend(ruleset.warnings[:5])

    if project_index is not None and groups:
        result.project = project_index.root
        for group in groups:
            group.code_refs = project_index.references_for_group(group)
            if group.code_refs:
                group.confidence = min(0.9, max(group.confidence, 0.6))
                if group.analyzer == "rules":
                    group.analyzer = "hybrid"
                elif group.analyzer == "heuristic":
                    group.analyzer = "heuristic+code"

    want_llm = settings.llm.enabled if use_llm is None else use_llm
    if want_llm and settings.llm_available():
        if llm_client is None:
            llm_client = LLMClient(settings.llm, settings.llm_api_key())
        result.llm_used = _apply_llm(groups, settings, llm_client, result)
        if result.llm_used and groups:
            try:
                summary = llm_client.summarize(result)
            except LLMError as exc:
                result.notes.append(f"LLM 总结失败：{exc}")
            else:
                if summary:
                    result.notes.insert(0, f"AI 总结：{summary}")

    _sort_groups(groups)
    limit = settings.general.max_groups
    if len(groups) > limit:
        result.notes.append(f"共发现 {len(groups)} 个错误组，报告只展示最严重的 {limit} 个")
    result.groups = groups[:limit]
    return result


def analyze_text(
    text: str,
    settings: Settings,
    *,
    source: str = "<text>",
    project_index: ProjectIndex | None = None,
    ruleset: RuleSet | None = None,
    use_llm: bool | None = None,
) -> AnalysisResult:
    return analyze_sources(
        [(source, text)],
        settings,
        project_index=project_index,
        ruleset=ruleset,
        use_llm=use_llm,
    )


def analyze_paths(
    paths: list[str],
    settings: Settings,
    *,
    stdin_text: str | None = None,
    use_llm: bool | None = None,
    index_project: bool = True,
) -> AnalysisResult:
    files = collect_log_files(paths)
    sources = read_sources(files, stdin_text)
    project_index = None
    warnings: list[str] = []
    if index_project:
        project_index, warnings = build_project_index(settings)
    result = analyze_sources(
        sources,
        settings,
        project_index=project_index,
        use_llm=use_llm,
    )
    result.notes.extend(warnings)
    return result
