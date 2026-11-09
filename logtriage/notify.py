"""通知：控制台 / 飞书 / 钉钉 / 企业微信 / Telegram / 邮件 / 通用 webhook。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .config import ChannelSettings, NotifySettings
from .models import AnalysisResult, ErrorGroup
from .render import render_markdown, render_text
from .utils import atomic_write_text, severity_rank, truncate

STATE_FILE_NAME = ".logtriage_state.json"


class NotifyError(RuntimeError):
    pass


@dataclass
class ChannelResult:
    kind: str
    ok: bool
    detail: str = ""


def _option(options: dict[str, Any], key: str, default: Any = None) -> Any:
    value = options.get(key, default)
    return default if value is None else value


def _secret(options: dict[str, Any], key: str, env_key: str) -> str:
    direct = str(options.get(key) or "").strip()
    if direct:
        return direct
    env_name = str(options.get(env_key) or "").strip()
    if env_name:
        return str(os.environ.get(env_name, "")).strip()
    return ""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


class CooldownStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path else Path.cwd() / STATE_FILE_NAME

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))


def _filter_cooldown(
    groups: list[ErrorGroup],
    minutes: float,
    store: CooldownStore,
) -> tuple[list[ErrorGroup], dict[str, Any]]:
    state = store.load()
    notified = state.get("notified") if isinstance(state.get("notified"), dict) else {}
    now = time.time()
    if minutes <= 0:
        return groups, state
    selected: list[ErrorGroup] = []
    for group in groups:
        entry = notified.get(group.fingerprint)
        if isinstance(entry, dict):
            try:
                last = float(entry.get("at", 0))
            except (TypeError, ValueError):
                last = 0.0
            if now - last < minutes * 60:
                continue
        selected.append(group)
    return selected, state


def _mark_notified(groups: list[ErrorGroup], store: CooldownStore, state: dict[str, Any]) -> None:
    notified = state.get("notified") if isinstance(state.get("notified"), dict) else {}
    now = time.time()
    for group in groups:
        notified[group.fingerprint] = {"at": now, "count": group.count, "title": group.title[:160]}
    state["notified"] = notified
    store.save(state)


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = 20.0) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except OSError:
            pass
        raise NotifyError(f"HTTP {exc.code}: {truncate(body, 200)}") from exc
    except urllib.error.URLError as exc:
        raise NotifyError(f"网络错误：{exc.reason}") from exc


def _check_json_response(body: str) -> None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    for code_key in ("code", "errcode", "error_code"):
        code = data.get(code_key)
        if code not in (None, 0, "0"):
            message = data.get("msg") or data.get("errmsg") or data.get("message") or ""
            raise NotifyError(f"接口返回错误 code={code} {message}")
    if data.get("ok") is False:
        raise NotifyError(str(data.get("description") or "接口返回 ok=false"))


def _signed_url(url: str, style: str, secret: str) -> str:
    if not secret:
        return url
    if style == "feishu":
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        return url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"timestamp": timestamp, "sign": sign})
    if style == "dingtalk":
        timestamp = str(int(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        sign = base64.b64encode(
            hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        return url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"timestamp": timestamp, "sign": sign})
    return url


def _send_webhook(
    options: dict[str, Any],
    subject: str,
    text: str,
    markdown: str,
) -> str:
    url = str(_option(options, "url", "")).strip()
    if not url:
        raise NotifyError("webhook 渠道缺少 url")
    style = str(_option(options, "style", "generic")).lower()
    secret = _secret(options, "secret", "secret_env")
    target = _signed_url(url, style, secret)

    if style == "feishu":
        payload = {"msg_type": "text", "content": {"text": f"{subject}\n{text}"[:4000]}}
    elif style == "dingtalk":
        payload = {"msgtype": "markdown", "markdown": {"title": subject, "text": f"### {subject}\n\n{markdown}"[:4000]}}
    elif style == "wecom":
        payload = {"msgtype": "markdown", "markdown": {"content": f"**{subject}**\n\n{markdown}"[:4000]}}
    else:
        payload = {
            "title": subject,
            "text": text,
            "markdown": markdown,
            "severity": _option(options, "severity", ""),
        }
    status, body = _http_post_json(target, payload)
    _check_json_response(body)
    return f"HTTP {status}"


def _send_telegram(options: dict[str, Any], subject: str, text: str) -> str:
    token = _secret(options, "token", "token_env")
    chat_id = str(_option(options, "chat_id", "")).strip()
    if not token:
        raise NotifyError("telegram 渠道缺少 token/token_env")
    if not chat_id:
        raise NotifyError("telegram 渠道缺少 chat_id")
    api_base = str(_option(options, "api_base", "https://api.telegram.org")).rstrip("/")
    payload = {
        "chat_id": chat_id,
        "text": f"{subject}\n{text}"[:4000],
        "disable_web_page_preview": True,
    }
    status, body = _http_post_json(f"{api_base}/bot{token}/sendMessage", payload)
    try:
        data = json.loads(body)
        if data.get("ok") is False:
            raise NotifyError(str(data.get("description") or "Telegram 发送失败"))
    except json.JSONDecodeError:
        pass
    return f"HTTP {status}"


def _send_email(options: dict[str, Any], subject: str, text: str, markdown: str) -> str:
    host = str(_option(options, "host", "")).strip()
    if not host:
        raise NotifyError("email 渠道缺少 host")
    port = int(_option(options, "port", 465 if _option(options, "ssl", True) else 25))
    sender = str(_option(options, "from", "") or _option(options, "username", "")).strip()
    recipients = _as_list(_option(options, "to", []))
    if not sender:
        raise NotifyError("email 渠道缺少 from/username")
    if not recipients:
        raise NotifyError("email 渠道缺少 to")
    password = _secret(options, "password", "password_env")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    message.add_alternative(
        "<html><body><pre style=\"white-space:pre-wrap;font-family:Consolas,monospace\">"
        + _html_escape(markdown)
        + "</pre></body></html>",
        subtype="html",
    )

    use_ssl = bool(_option(options, "ssl", True))
    use_starttls = bool(_option(options, "starttls", False))
    try:
        if use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            if use_starttls:
                server.starttls(context=ssl.create_default_context())
        with server:
            if password:
                server.login(str(_option(options, "username", sender)), password)
            server.send_message(message, from_addr=sender, to_addrs=recipients)
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"邮件发送失败：{exc}") from exc
    return "已发送"


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _send_channel(
    channel: ChannelSettings,
    subject: str,
    text: str,
    markdown: str,
) -> str:
    kind = (channel.kind or "").lower()
    options = channel.options or {}
    if kind == "console":
        print(text, flush=True)
        return "已输出到控制台"
    if kind in ("webhook", "feishu", "dingtalk", "wecom"):
        if kind in ("feishu", "dingtalk", "wecom") and "style" not in options:
            options = dict(options)
            options["style"] = kind
        return _send_webhook(options, subject, text, markdown)
    if kind == "telegram":
        return _send_telegram(options, subject, text)
    if kind == "email":
        return _send_email(options, subject, text, markdown)
    raise NotifyError(f"未知通知渠道：{channel.kind}")


def build_subject(result: AnalysisResult, groups: list[ErrorGroup], settings: NotifySettings) -> str:
    return (
        f"{settings.subject_prefix} {result.top_severity.upper()} "
        f"{len(groups)} 个错误组（{sum(group.count for group in groups)} 次）"
    )


def notify(
    result: AnalysisResult,
    settings: NotifySettings,
    *,
    state_path: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> list[ChannelResult]:
    """按严重度过滤 + 冷却去重后，把报告发到所有启用的渠道。"""
    if not settings.enabled:
        return []
    threshold = severity_rank(settings.min_severity)
    groups = [group for group in result.groups if severity_rank(group.severity) >= threshold]
    if not groups:
        return [ChannelResult("notify", False, f"没有达到 {settings.min_severity} 的错误组，未发送")]

    store = CooldownStore(state_path)
    groups, state = _filter_cooldown(groups, settings.cooldown_minutes, store)
    if not groups:
        return [ChannelResult("notify", False, "所有错误组都在冷却期内，未发送")]

    subset = replace(result, groups=groups)
    subject = build_subject(subset, groups, settings)
    text = render_text(subset)
    markdown = render_markdown(subset)
    if len(text) > 3800:
        text = text[:3790] + "\n…（内容过长已截断）"

    if dry_run:
        return [ChannelResult("dry-run", True, f"将发送：{subject}")]

    enabled_channels = [channel for channel in settings.channels if channel.enabled]
    if not enabled_channels:
        return [ChannelResult("notify", False, "没有启用任何通知渠道")]

    results: list[ChannelResult] = []
    any_success = False
    for channel in enabled_channels:
        try:
            detail = _send_channel(channel, subject, text, markdown)
        except (NotifyError, OSError, ValueError) as exc:
            results.append(ChannelResult(channel.kind, False, str(exc)))
        else:
            any_success = True
            results.append(ChannelResult(channel.kind, True, detail))
    if any_success:
        _mark_notified(groups, store, state)
    return results
