"""无第三方依赖的小工具。"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_WS_RE = re.compile(r"\s+")

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def strip_ansi(text: str) -> str:
    """去掉终端颜色/控制序列，避免污染指纹和报告。"""
    return _ANSI_RE.sub("", text)


def read_text_safe(path: str | os.PathLike[str], max_bytes: int | None = None) -> str:
    """尽量别因为编码问题崩掉：UTF-8 / GB18030 / Latin-1 依次尝试。"""
    data = Path(path).read_bytes()
    if max_bytes is not None:
        data = data[:max_bytes]
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def decode_bytes_safe(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def ensure_utf8_stdout() -> None:
    """Windows 终端默认 GBK，中文报告会花式报错；能改就改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def local_now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def isoformat(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def truncate(text: Any, limit: int = 160, suffix: str = "…") -> str:
    if text is None:
        return ""
    value = _WS_RE.sub(" ", str(text)).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def severity_rank(value: str | None) -> int:
    return SEVERITY_RANK.get((value or "").lower(), 0)


def highest_severity(*values: str | None) -> str:
    best = "low"
    for value in values:
        if value and severity_rank(value) > severity_rank(best):
            best = value.lower()
    return best


def short_type_name(name: str | None) -> str:
    if not name:
        return ""
    return name.rsplit(".", 1)[-1]


def normalize_slashes(path: str) -> str:
    return path.replace("\\", "/")


def to_jsonable(value: Any) -> Any:
    """dataclass / datetime / dict 递归转成可 JSON 序列化的结构。"""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


def json_dumps(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, indent=indent)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".logtriage-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return target


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural_form or singular + 's')}"
