"""错误聚合：把「同一个根因」的重复报错压成一组。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .detect import is_fatal_text
from .models import ErrorGroup, LogEvent
from .utils import highest_severity, short_type_name, truncate

_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ISO_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{16,}\b")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_ID_RE = re.compile(r"\b(?:request[_-]?id|trace[_-]?id|span[_-]?id|user[_-]?id|id)=([A-Za-z0-9_.:@-]{4,})", re.I)
_QUOTED_RE = re.compile(r"'[^'\n]{0,64}'|\"[^\"\n]{0,64}\"")
_WS_RE = re.compile(r"\s+")


def normalize_message(text: str | None) -> str:
    """把易变的部分替换成占位符，让同类错误落到同一个指纹里。"""
    if not text:
        return ""
    value = str(text).strip()
    value = _UUID_RE.sub("<uuid>", value)
    value = _ISO_TS_RE.sub("<ts>", value)
    value = _IP_RE.sub("<ip>", value)
    value = _ID_RE.sub("<id>", value)
    value = _HEX_RE.sub("<hex>", value)
    value = _NUMBER_RE.sub("<n>", value)
    value = _QUOTED_RE.sub("<q>", value)
    value = _WS_RE.sub(" ", value).strip().lower()
    return value[:400]


def fingerprint_for(event: LogEvent) -> str:
    type_name = (short_type_name(event.exception_type) or event.level or "log").lower()
    text = event.exception_message or event.message
    key = f"{type_name}|{normalize_message(text)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _make_title(event: LogEvent) -> str:
    if event.exception_type:
        type_name = short_type_name(event.exception_type)
        message = event.exception_message or event.message
        title = f"{type_name}: {message}" if message else type_name
        return truncate(title, 140)
    return truncate(event.message or event.raw, 140)


def _severity_for(group: ErrorGroup) -> str:
    if not group.events:
        return "medium"
    sample = group.events[0]
    text = " ".join(event.raw[:2000] for event in group.events[:5])
    if is_fatal_text(text):
        return "critical"
    if any(status and 500 <= status < 600 for status in (event.http_status for event in group.events)):
        return "high"
    levels = {(event.level or "").upper() for event in group.events}
    if "CRITICAL" in levels:
        return "critical"
    if "ERROR" in levels:
        return "high"
    if sample.is_error:
        return "high"
    if sample.is_warning:
        return "medium"
    return "low"


def group_events(events: list[LogEvent], max_events_per_group: int = 500) -> list[ErrorGroup]:
    groups: dict[str, ErrorGroup] = {}
    for event in events:
        fingerprint = fingerprint_for(event)
        group = groups.get(fingerprint)
        if group is None:
            group = ErrorGroup(
                fingerprint=fingerprint,
                title=_make_title(event),
                exception_type=event.exception_type,
                sample_message=event.exception_message or event.message,
                first_seen=event.timestamp,
                last_seen=event.timestamp,
            )
            groups[fingerprint] = group
        group.count += 1
        if len(group.events) < max_events_per_group:
            group.events.append(event)
        if event.timestamp:
            first = group.first_seen
            last = group.last_seen
            if first is None or event.timestamp < first:
                group.first_seen = event.timestamp
            if last is None or event.timestamp > last:
                group.last_seen = event.timestamp
        if event.stack and not group.stack:
            group.stack = event.stack
        group.severity = highest_severity(group.severity, _severity_for(group))

    return list(groups.values())


def annotate_event_times(events: list[LogEvent]) -> tuple[datetime | None, datetime | None]:
    stamps = [event.timestamp for event in events if event.timestamp]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)
