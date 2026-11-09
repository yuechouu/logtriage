"""核心数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Frame:
    """一条堆栈帧（已经尽量从日志里解析出来）。"""

    file: Optional[str] = None
    line: Optional[int] = None
    function: Optional[str] = None
    raw: str = ""

    def where(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        if self.file:
            return self.file
        if self.function:
            return self.function
        return "<unknown>"


@dataclass
class LogEvent:
    """一条归一化后的日志记录（可能来自多行）。"""

    index: int = 0
    raw: str = ""
    message: str = ""
    level: Optional[str] = None
    timestamp: Optional[datetime] = None
    logger: Optional[str] = None
    service: Optional[str] = None
    thread: Optional[str] = None
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    stack: list[Frame] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    line_no: int = 0
    is_error: bool = False
    is_warning: bool = False
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    http_status: Optional[int] = None

    @property
    def headline(self) -> str:
        """用于标题/指纹的一句话。"""
        if self.exception_type:
            if self.exception_message:
                return f"{self.exception_type}: {self.exception_message}"
            return self.exception_type
        return self.message

    def text_for_analysis(self, limit: int = 400) -> str:
        parts = [self.message]
        if self.exception_type:
            parts.append(self.headline)
        text = " | ".join(part for part in parts if part)
        return text[:limit]


@dataclass
class CodeRef:
    """日志指向的一段项目代码。"""

    path: str
    line: int
    kind: str = "stack"  # stack | message | symbol | raise
    symbol: Optional[str] = None
    snippet: list[str] = field(default_factory=list)
    score: float = 1.0
    note: str = ""

    def label(self) -> str:
        if not self.line:
            location = self.path
        else:
            location = f"{self.path}:{self.line}"
        if self.symbol:
            return f"{location} ({self.symbol})"
        return location


@dataclass
class ErrorGroup:
    """同一个根因的一类报错。"""

    fingerprint: str
    title: str
    exception_type: Optional[str]
    sample_message: str
    count: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    severity: str = "medium"
    events: list[LogEvent] = field(default_factory=list)
    stack: list[Frame] = field(default_factory=list)
    code_refs: list[CodeRef] = field(default_factory=list)
    rule_id: Optional[str] = None
    rule_title: Optional[str] = None
    root_cause: str = ""
    code_analysis: str = ""
    fixes: list[str] = field(default_factory=list)
    confidence: float = 0.5
    analyzer: str = "heuristic"  # heuristic | rules | llm | hybrid
    notes: list[str] = field(default_factory=list)

    @property
    def evidence(self) -> list[LogEvent]:
        """挑三条有代表性的日志：最早、中间、最近。"""
        if len(self.events) <= 3:
            return list(self.events)
        middle = self.events[len(self.events) // 2]
        return [self.events[0], middle, self.events[-1]]


@dataclass
class AnalysisResult:
    sources: list[str] = field(default_factory=list)
    project: Optional[str] = None
    generated_at: Optional[datetime] = None
    total_lines: int = 0
    total_events: int = 0
    error_events: int = 0
    groups: list[ErrorGroup] = field(default_factory=list)
    level_counts: dict[str, int] = field(default_factory=dict)
    services: dict[str, int] = field(default_factory=dict)
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    llm_used: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def top_severity(self) -> str:
        best = "low"
        for group in self.groups:
            from .utils import severity_rank

            if severity_rank(group.severity) > severity_rank(best):
                best = group.severity
        return best

    def count_at_least(self, severity: str) -> int:
        from .utils import severity_rank

        threshold = severity_rank(severity)
        return sum(1 for group in self.groups if severity_rank(group.severity) >= threshold)
