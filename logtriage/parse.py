"""日志解析：把乱七八糟的文本切成一条条 LogEvent。

支持：
- 普通单行日志（时间戳 + 等级 + logger + 消息）；
- Python 多行 Traceback、Java/C# 堆栈、Node 堆栈、Go panic；
- 单行/多行 JSON 日志；
- Nginx/Apache 访问日志（能从状态码看出 5xx）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .models import Frame, LogEvent
from .utils import strip_ansi, truncate

LEVEL_TOKENS = (
    "TRACE",
    "DEBUG",
    "INFO",
    "NOTICE",
    "WARN",
    "WARNING",
    "ERROR",
    "ERR",
    "FATAL",
    "CRITICAL",
    "SEVERE",
    "PANIC",
    "ALERT",
    "EMERGENCY",
)
LEVEL_CANON = {
    "WARNING": "WARN",
    "ERR": "ERROR",
    "SEVERE": "ERROR",
    "FATAL": "CRITICAL",
    "PANIC": "CRITICAL",
    "ALERT": "CRITICAL",
    "EMERGENCY": "CRITICAL",
    "NOTICE": "INFO",
}

_LEVEL_ALT = "|".join(LEVEL_TOKENS)
_BARE_LEVEL_RE = re.compile(rf"^[\[\(]?(?:{_LEVEL_ALT})[\]\)]?(?=[:\s\-|,])", re.I)
_LEVEL_HEAD_RE = re.compile(rf"^(?P<lvl>{_LEVEL_ALT})\b[:\s\-|,]*", re.I)
_LOGGER_AT_START_RE = re.compile(
    r"^(?P<logger>[A-Za-z_][\w$.-]*(?:\.[\w$]+)+|[a-z][\w-]{2,30})\s+[-:]\s+"
)


def _looks_like_service_prefix(after: str) -> bool:
    """True when a [bracket] is followed by `logger - message` (optionally after LEVEL)."""
    if _LOGGER_AT_START_RE.match(after):
        return True
    match = _LEVEL_HEAD_RE.match(after)
    return bool(match and _LOGGER_AT_START_RE.match(after[match.end() :]))

_TS_AT_START = re.compile(
    r"^(?:"
    r"\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
    r"|\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?"
    r"|\[\d{4}[-/]\d{2}[-/]\d{2}[^\]]{0,40}\]"
    r")"
)

_TS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"),
    re.compile(r"^(?P<ts>\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    re.compile(r"^(?P<ts>\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    re.compile(r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2})"),
    re.compile(r"^(?P<ts>\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"),
    re.compile(r"^\[(?P<ts>[^\]\n]{10,40})\]"),
]

_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

_ACCESS_RE = re.compile(
    r'^(?P<ip>\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]{3,})\s+\S+\s+\S+\s+'
    r'\[(?P<ts>[^\]]+)\]\s+"(?P<request>[^"]*)"\s+(?P<status>\d{3})\s+(?P<size>\S+)'
)

_EXCEPTION_RE = re.compile(
    r"^(?:Caused by:\s*)?(?:(?:[A-Za-z_$][\w$]*\.)+)?"
    r"(?P<type>[A-Z][\w$]*(?:Exception|Error|Throwable|Failure|Warning))"
    r"(?::\s*(?P<msg>.*))?$"
)
_THREAD_EXCEPTION_RE = re.compile(
    r'^Exception in thread "[^"]*"\s+(?:(?:[A-Za-z_$][\w$]*\.)+)?'
    r"(?P<type>[A-Z][\w$]*(?:Exception|Error|Throwable))(?::\s*(?P<msg>.*))?$"
)
_PANIC_RE = re.compile(r"^panic:\s*(?P<msg>.+)$")
_UNHANDLED_RE = re.compile(
    r"^(?:Unhandled exception\.?\s*)(?:(?:[A-Za-z_$][\w$]*\.)+)?"
    r"(?P<type>[A-Z][\w$]*(?:Exception|Error))(?::\s*(?P<msg>.*))?$"
)

_FRAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?'),
    re.compile(r"^\s*at\s+(?P<func>[\w$.<>/]+)\((?P<file>[^:()]+\.\w+):(?P<line>\d+)\)"),
    re.compile(r"^\s*at\s+(?P<func>.*?)\s*\((?P<file>.+?):(?P<line>\d+):(?P<col>\d+)\)"),
    re.compile(r"^\s*at\s+(?P<file>[A-Za-z]:[\\/].+?|/[^:()]+?):(?P<line>\d+):(?P<col>\d+)\s*$"),
    re.compile(r"^\s*at\s+(?P<func>.*?)\s+in\s+(?P<file>.+?):line\s+(?P<line>\d+)"),
    re.compile(
        r"^\s*(?P<file>.+?\.(?:py|java|kt|kts|js|jsx|ts|tsx|go|cs|rb|php|rs|scala|swift|c|cc|cpp|h|hpp|m))"
        r":(?P<line>\d+)(?::\d+)?(?:\s+.*)?$"
    ),
    re.compile(r"^\s*#\d+\s+0x[0-9A-Fa-f]+\s+in\s+(?P<func>\S+)\s+at\s+(?P<file>[^:]+):(?P<line>\d+)"),
]

_TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    "Caused by:",
    "Exception in thread",
    "panic:",
    "goroutine ",
)


def canonical_level(level: str | None) -> str | None:
    if not level:
        return None
    upper = level.strip().upper()
    return LEVEL_CANON.get(upper, upper)


def _localize(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0) if dt.microsecond else dt


def parse_timestamp(raw: Any) -> datetime | None:
    """尽可能宽容地解析时间戳，失败就返回 None，绝不抛异常。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value > 1e11:  # 毫秒
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip().strip("[]")
    if not text:
        return None
    if re.fullmatch(r"\d{10,17}(?:\.\d+)?", text):
        return parse_timestamp(float(text))

    candidate = text.replace(",", ".")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", candidate)
    try:
        return _localize(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return _localize(datetime.strptime(candidate, fmt))
        except ValueError:
            continue

    # Apache/Nginx access log: 10/Nov/2026:12:00:00 +0800
    match = re.match(r"(\d{1,2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2})\s*([+-]\d{4})?", text)
    if match and match.group(2) in _MONTHS:
        day, mon, year, hour, minute, second, tz = match.groups()
        try:
            dt = datetime(int(year), _MONTHS[mon], int(day), int(hour), int(minute), int(second))
        except ValueError:
            return None
        if not tz:
            return dt
        sign = 1 if tz[0] == "+" else -1
        offset = sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
        # 10:07+08:00 == 02:07Z；先换回 UTC，再转本机时区。
        return _localize(dt.replace(tzinfo=timezone.utc) - offset)

    # Syslog: Nov  9 12:00:00（补当前年份）
    match = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})", text)
    if match and match.group(1) in _MONTHS:
        mon, day, hour, minute, second = match.groups()
        try:
            return datetime(datetime.now().year, _MONTHS[mon], int(day), int(hour), int(minute), int(second))
        except ValueError:
            return None

    # 只有时间：补今天
    match = re.match(r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?", text)
    if match:
        hour, minute, second, fraction = match.groups()
        now = datetime.now()
        try:
            return now.replace(
                hour=int(hour),
                minute=int(minute),
                second=int(second),
                microsecond=int((fraction or "0").ljust(6, "0")[:6]),
            )
        except ValueError:
            return None
    return None


def _consume_timestamp(line: str) -> tuple[datetime | None, str]:
    for pattern in _TS_PATTERNS:
        match = pattern.match(line)
        if match:
            return parse_timestamp(match.group("ts")), line[match.end() :]
    return None, line


def _has_traceback_context(lines: Iterable[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(_TRACEBACK_MARKERS):
            return True
        if stripped.startswith('File "') or stripped.startswith("at "):
            return True
        if "Caused by:" in stripped:
            return True
    return False


def _starts_new_record(line: str) -> bool:
    if _ACCESS_RE.match(line):
        return True
    if _TS_AT_START.match(line):
        return True
    if line.lstrip().startswith("{"):
        return True
    if _BARE_LEVEL_RE.match(line):
        return True
    return False


def _is_log_header(line: str) -> bool:
    """这一行看起来像一条新日志的头部（时间戳或裸等级开头）。"""
    return bool(_TS_AT_START.match(line) or _BARE_LEVEL_RE.match(line))


def _is_continuation(line: str, current: list[str]) -> bool:
    if line[:1] in (" ", "\t"):
        return True
    if current and current[0].lstrip().startswith("{") and re.match(r"^[}\]]", line):
        return True
    # 日志头部下一行直接跟异常类型（没有缩进、没有 traceback 头），
    # 例如 "ERROR ... cache lost" + "RedisConnectionError: ..."。
    if current and _is_log_header(current[0]) and (
        _EXCEPTION_RE.match(line.strip()) or _THREAD_EXCEPTION_RE.match(line.strip())
    ):
        return True
    if re.match(
        r"^(?:Caused by:|Suppressed:|\.\.\. \d+ more\b|Traceback \(most recent call last\):|"
        r"During handling of the above exception|The above exception|goroutine \d+|"
        r"panic:|fatal error:|runtime error:)",
        line,
    ):
        return True
    if line.startswith("at ") or line.startswith("at\t"):
        return True
    if re.match(r"^#\d+\s", line):
        return True
    if _has_traceback_context(current) and (
        _EXCEPTION_RE.match(line.strip()) or _THREAD_EXCEPTION_RE.match(line.strip())
    ):
        return True
    return False


def split_blocks(text: str) -> list[tuple[int, list[str]]]:
    """按「空行 / 新记录起点」切分，同时把多行堆栈粘回同一个事件。"""
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start_line = 0
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = strip_ansi(raw_line)
        if not line.strip():
            if current and _has_traceback_context(current):
                # 堆栈中间的空白行不能把同一个异常切断。
                continue
            if current:
                blocks.append((start_line, current))
                current = []
            continue
        if not current:
            current = [line]
            start_line = line_no
            continue
        if _starts_new_record(line) or not _is_continuation(line, current):
            blocks.append((start_line, current))
            current = [line]
            start_line = line_no
        else:
            current.append(line)
    if current:
        blocks.append((start_line, current))
    return blocks


def _split_exception_text(value: str) -> tuple[str | None, str | None]:
    """把 'java.lang.NullPointerException: xxx' 拆成类型和消息。"""
    text = str(value).strip()
    if not text:
        return None, None
    match = _EXCEPTION_RE.match(text) or _THREAD_EXCEPTION_RE.match(text)
    if match:
        return match.group("type"), (match.group("msg") or "").strip() or None
    if ":" in text:
        head, tail = text.split(":", 1)
        head = head.strip()
        if re.fullmatch(r"(?:[A-Za-z_$][\w$]*\.)*[A-Z][\w$]*(?:Exception|Error|Throwable|Failure)", head):
            return head, tail.strip() or None
    return None, None


def _parse_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str) and re.fullmatch(r"[1-5]\d{2}", value.strip()):
        return int(value.strip())
    return None


def _first_present(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, "", [], {}):
            return data[key]
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _fill_from_json(event: LogEvent, data: dict[str, Any]) -> bool:
    message = _first_present(data, ("message", "msg", "log", "body", "event", "text"))
    level = _first_present(data, ("level", "severity", "lvl", "log.level", "log_level"))
    timestamp = _first_present(data, ("timestamp", "time", "ts", "@timestamp", "datetime", "date", "created_at"))
    logger = _first_present(data, ("logger", "logger_name", "name", "module"))
    service = _first_present(data, ("service", "service_name", "app", "application", "component"))
    thread = _first_present(data, ("thread", "thread_name", "threadName"))
    stack = _first_present(data, ("stack", "traceback", "stacktrace", "stack_trace", "exception.stacktrace"))

    error = data.get("error") if isinstance(data.get("error"), dict) else None
    exc = data.get("exception") if isinstance(data.get("exception"), dict) else None
    exc_type = _first_present(data, ("exception_type", "error.type", "error_type"))
    exc_message = _first_present(data, ("exception_message", "error.message", "error_message", "err"))
    if error:
        exc_type = exc_type or error.get("type") or error.get("name") or error.get("class")
        exc_message = exc_message or error.get("message") or error.get("msg")
        stack = stack or error.get("stack") or error.get("trace")
    if exc:
        exc_type = exc_type or exc.get("type") or exc.get("class")
        exc_message = exc_message or exc.get("message") or exc.get("msg")
        stack = stack or exc.get("stacktrace") or exc.get("stack")

    if isinstance(level, int):
        level = {10: "TRACE", 20: "DEBUG", 30: "INFO", 40: "WARN", 50: "ERROR", 60: "CRITICAL"}.get(level)
    if level:
        event.level = canonical_level(_as_text(level))
    if timestamp:
        event.timestamp = parse_timestamp(timestamp)
    if message is not None:
        event.message = _as_text(message)
    if logger:
        event.logger = _as_text(logger)
    if service:
        event.service = _as_text(service)
    if thread:
        event.thread = _as_text(thread)

    if exc_type:
        parsed_type, parsed_msg = _split_exception_text(_as_text(exc_type))
        event.exception_type = parsed_type or _as_text(exc_type)
        if exc_message:
            event.exception_message = _as_text(exc_message)
        elif parsed_msg:
            event.exception_message = parsed_msg
    elif isinstance(data.get("exception"), str):
        parsed_type, parsed_msg = _split_exception_text(data["exception"])
        event.exception_type, event.exception_message = parsed_type, parsed_msg
    elif exc_message and not event.exception_type:
        parsed_type, parsed_msg = _split_exception_text(_as_text(exc_message))
        if parsed_type:
            event.exception_type, event.exception_message = parsed_type, parsed_msg

    http = data.get("http")
    if isinstance(http, dict):
        status = _parse_status(http.get("status") or http.get("status_code") or http.get("code"))
        if status:
            event.http_status = status
            event.extra["http_status"] = status
    status = _parse_status(data.get("status_code") or data.get("http_status"))
    if status:
        event.http_status = status

    for key in ("method", "path", "url", "request_id", "trace_id", "traceId", "span_id", "user_id", "tenant_id"):
        value = data.get(key)
        if isinstance(value, (str, int, float)):
            event.extra[key] = value

    if isinstance(stack, str) and stack.strip():
        _attach_frames(event, stack.splitlines())
    elif isinstance(stack, list):
        _attach_frames(event, [_as_text(item) for item in stack])

    if not event.message:
        event.message = truncate(_as_text(data), 500)
    return True


def _attach_frames(event: LogEvent, lines: Iterable[str]) -> None:
    seen: set[tuple[str | None, int | None, str | None]] = set()
    for line in lines:
        for pattern in _FRAME_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            groups = match.groupdict()
            file = (groups.get("file") or "").strip().strip('"') or None
            raw_line = groups.get("line")
            line_no = int(raw_line) if raw_line and str(raw_line).isdigit() else None
            func = (groups.get("func") or "").strip() or None
            key = (file, line_no, func)
            if key in seen:
                break
            seen.add(key)
            event.stack.append(Frame(file=file, line=line_no, function=func, raw=line.strip()))
            break


def _extract_exception(lines: list[str]) -> tuple[str | None, str | None]:
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = _PANIC_RE.match(stripped)
        if match:
            message = match.group("msg").strip()
            if message.lower().startswith("runtime error:"):
                return "panic", message
            return "panic", message
        match = _THREAD_EXCEPTION_RE.match(stripped)
        if match:
            return match.group("type"), (match.group("msg") or "").strip() or None
        match = _UNHANDLED_RE.match(stripped)
        if match:
            return match.group("type"), (match.group("msg") or "").strip() or None
        match = _EXCEPTION_RE.match(stripped)
        if match:
            return match.group("type"), (match.group("msg") or "").strip() or None
    return None, None


def _parse_access_line(line: str, event: LogEvent) -> bool:
    match = _ACCESS_RE.match(line)
    if not match:
        return False
    event.timestamp = parse_timestamp(match.group("ts"))
    status = int(match.group("status"))
    event.http_status = status
    event.extra.update(
        {
            "client_ip": match.group("ip"),
            "request": match.group("request"),
            "bytes": match.group("size"),
            "http_status": status,
        }
    )
    if 500 <= status < 600:
        event.level = "ERROR"
    elif status in (408, 429):
        event.level = "WARN"
    else:
        event.level = "INFO"
    event.message = f'{match.group("request")} -> {status}'
    return True


def _extract_key_values(event: LogEvent, text: str) -> None:
    for match in re.finditer(
        r"\b(service|app|application|component|logger|status|status_code|http_status|"
        r"method|path|request_id|trace_id|traceId|span_id|user_id|tenant_id)=(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        text,
    ):
        key = match.group(1)
        value = match.group(2).strip("\"'")
        if key in ("service", "app", "application", "component") and not event.service:
            event.service = value
        elif key == "logger" and not event.logger:
            event.logger = value
        elif key in ("status", "status_code", "http_status"):
            status = _parse_status(value)
            if status and not event.http_status:
                event.http_status = status
        else:
            event.extra[key] = value


def _parse_head(line: str, event: LogEvent) -> None:
    if _parse_access_line(line, event):
        return

    timestamp, rest = _consume_timestamp(line)
    if timestamp:
        event.timestamp = timestamp
    rest = rest.lstrip(" |:-\t")

    # 前缀可以是 "[thread] [LEVEL]" / "INFO [service]" / "[service] INFO" 等组合。
    # 先吃掉可能出现的裸 LEVEL，再循环处理方括号，最后再吃一次 LEVEL。
    level_match = _LEVEL_HEAD_RE.match(rest)
    if level_match:
        event.level = canonical_level(level_match.group("lvl"))
        rest = rest[level_match.end() :]

    for _ in range(3):
        match = re.match(r"^\[([^\]]{1,80})\]\s*", rest)
        if not match:
            break
        content = match.group(1).strip()
        after = rest[match.end() :]
        if content.upper() in LEVEL_TOKENS:
            event.level = canonical_level(content)
        elif (
            event.service is None
            and _looks_like_service_prefix(after)
            and not re.search(r"\d", content)
        ):
            # 例如 "ERROR [order-api] app.orders - ..."：方括号里是服务名
            event.service = content
        elif event.thread is None:
            event.thread = content
        rest = after

    # "[order-api] INFO app.orders - ..." 形式：LEVEL 在服务名之后
    level_match = _LEVEL_HEAD_RE.match(rest)
    if level_match:
        event.level = canonical_level(level_match.group("lvl"))
        rest = rest[level_match.end() :]
        if event.service is None and event.thread and _LOGGER_AT_START_RE.match(rest):
            # 之前把服务名误当成 thread，这里纠正
            event.service = event.thread
            event.thread = None

    match = re.match(r"^\((?:thread\s*)?([^)]{1,60})\)\s*", rest)
    if match and event.thread is None:
        event.thread = match.group(1).strip()
        rest = rest[match.end() :]

    _extract_key_values(event, rest[:400])

    match = re.match(r"^(?P<logger>[A-Za-z_][\w$.-]*(?:\.[\w$]+)+)\s+[-:]\s+", rest)
    if not match:
        match = re.match(r"^(?P<logger>[a-z][\w-]{2,30})\s+-\s+", rest)
    if match:
        event.logger = match.group("logger")
        if not event.service:
            event.service = event.logger.split(".")[0]
        rest = rest[match.end() :]

    event.message = rest.strip() or line.strip()


def _parse_block(raw_lines: list[str], start_line: int, index: int, source: str) -> LogEvent:
    raw = "\n".join(raw_lines)
    event = LogEvent(index=index, raw=raw, source=source, line_no=start_line)

    from_json = False
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                from_json = _fill_from_json(event, data)
        except json.JSONDecodeError:
            from_json = False

    if not from_json:
        _parse_head(raw_lines[0], event)

    if not event.stack or not event.exception_type:
        _attach_frames(event, raw_lines)
    exception_type, exception_message = _extract_exception(raw_lines)
    if exception_type:
        event.exception_type = exception_type
        event.exception_message = exception_message
    elif not event.exception_type:
        parsed_type, parsed_msg = _split_exception_text(event.message)
        if parsed_type:
            event.exception_type = parsed_type
            if parsed_msg:
                event.exception_message = parsed_msg

    if not event.message:
        event.message = event.headline or truncate(raw, 500)
    if not event.service and event.logger:
        event.service = event.logger.split(".")[0]
    return event


def parse_text(text: str, source: str = "<memory>") -> list[LogEvent]:
    # 有些 Windows 工具写出的 UTF-8 文件带 BOM，先去掉，否则首行时间戳解析不到。
    text = text.lstrip("\ufeff")
    events: list[LogEvent] = []
    for index, (start_line, raw_lines) in enumerate(split_blocks(text)):
        events.append(_parse_block(raw_lines, start_line, index, source))
    return events
