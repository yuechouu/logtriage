"""错误识别：等级、关键词、HTTP 状态码综合打分。"""

from __future__ import annotations

import re

from .models import LogEvent

ERROR_LEVELS = {"ERROR", "CRITICAL"}
WARN_LEVELS = {"WARN"}

_FATAL_RE = re.compile(
    r"\b(?:outofmemoryerror|out of memory|oomkilled|oom-killed|killed|segmentation fault|"
    r"core dumped|panic|fatal|deadlock|data corruption|corrupt(?:ed)?\s+(?:file|database|index)|"
    r"disk (?:is )?full|no space left)\b",
    re.I,
)
_ERROR_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"traceback \(most recent call last\)", re.I), "traceback", 3),
    (re.compile(r"^\s*caused by:", re.I | re.M), "caused-by", 2),
    (re.compile(r"\b(?:fatal|panic|segmentation fault|core dumped)\b", re.I), "fatal", 3),
    (re.compile(r"\b(?:exception|uncaught|unhandled)\b", re.I), "exception", 2),
    (re.compile(r"\b(?:error|failed|failure)\b", re.I), "error", 2),
    (
        re.compile(
            r"\b(?:refused|timed out|timeout|deadline exceeded|denied|no such file|no space left|"
            r"too many open files|broken pipe|reset by peer|unavailable|unreachable|not found|"
            r"invalid|cannot|unable to|failed to|out of memory|killed|abort(?:ed)?|crash(?:ed)?)\b",
            re.I,
        ),
        "network-os",
        2,
    ),
    (re.compile(r"\b(?:warn|warning)\b", re.I), "warn", 1),
]

_HTTP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+[^"]*"\s+(?P<status>[1-5]\d{2})\s+'),
    re.compile(
        r"\b(?:status|status_code|http_status|statusCode|response_code)\"?\s*[=:]\s*\"?(?P<status>[1-5]\d{2})\b",
        re.I,
    ),
    re.compile(r"\bHTTP/\d(?:\.\d)?\s+(?P<status>[1-5]\d{2})\b", re.I),
]

_WARN_RE = re.compile(r"\bwarn(?:ing)?\b", re.I)
_EXC_TYPE_RE = re.compile(
    r"\b(?:[A-Za-z_$][\w$]*\.)*[A-Z][\w$]*(?:Exception|Error|Throwable|Failure)\b"
)


def extract_http_statuses(text: str) -> list[int]:
    statuses: list[int] = []
    for pattern in _HTTP_PATTERNS:
        for match in pattern.finditer(text):
            try:
                status = int(match.group("status"))
            except (TypeError, ValueError):
                continue
            if 100 <= status <= 599 and status not in statuses:
                statuses.append(status)
    # 5xx 优先，方便后续判断严重程度
    statuses.sort(key=lambda value: 0 if 500 <= value < 600 else 1)
    return statuses


def classify_event(event: LogEvent, include_warnings: bool = False) -> LogEvent:
    """给事件打上 is_error / is_warning / score。"""
    score = 0
    reasons: list[str] = []

    level = (event.level or "").upper()
    if level in ERROR_LEVELS:
        score += 3
        reasons.append(f"level={level}")
    if not event.exception_type:
        match = _EXC_TYPE_RE.search(event.message or event.raw)
        if match:
            event.exception_type = match.group(0)
    if event.exception_type:
        score += 3
        reasons.append("exception")
    if event.stack:
        score += 2
        reasons.append("stack")

    for pattern, name, weight in _ERROR_PATTERNS:
        if pattern.search(event.raw):
            score += weight
            if name not in reasons:
                reasons.append(name)

    if include_warnings and level in WARN_LEVELS:
        score += 2
        reasons.append("include-warnings")

    statuses = extract_http_statuses(event.raw)
    if statuses:
        if not event.http_status:
            event.http_status = statuses[0]
        if any(500 <= status < 600 for status in statuses):
            score += 3
            reasons.append("http-5xx")
        elif any(status in (408, 429) for status in statuses):
            score += 1
            reasons.append("http-4xx")

    event.score = score
    event.score_reasons = reasons
    event.is_error = score >= 2
    if event.is_error and level in WARN_LEVELS and "exception" not in reasons and "http-5xx" not in reasons:
        # WARN 里出现 refused/timeout 这类关键词仍然值得当错误看，但单独标一下
        event.score_reasons.append("warn-elevated")

    event.is_warning = (not event.is_error) and (level in WARN_LEVELS or bool(_WARN_RE.search(event.raw)))
    if include_warnings:
        event.is_warning = event.is_warning or level in WARN_LEVELS
    return event


def is_fatal_text(text: str) -> bool:
    return bool(_FATAL_RE.search(text))
