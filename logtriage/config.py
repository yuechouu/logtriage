"""配置加载：TOML + 默认值 + 少量环境变量覆盖。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import read_text_safe

SEVERITY_CHOICES = ("low", "medium", "high", "critical")
FAIL_ON_CHOICES = ("none", "low", "medium", "high", "critical")


class ConfigError(RuntimeError):
    """配置有问题，调用方应当明确告诉用户。"""


DEFAULTS: dict[str, Any] = {
    "general": {
        "include_warnings": False,
        "max_groups": 20,
        "fail_on": "high",
        "language": "zh",
        "extra_rules": [],
    },
    "project": {
        "root": "",
        "excludes": [],
        "max_file_kb": 512,
        "max_files": 8000,
    },
    "llm": {
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "",
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout": 45.0,
        "max_groups": 5,
        "temperature": 0.2,
    },
    "notify": {
        "enabled": False,
        "min_severity": "high",
        "cooldown_minutes": 30.0,
        "subject_prefix": "[logtriage]",
        "channels": [],
    },
}

EXAMPLE_TOML = """\
# logtriage 配置示例。复制为 logtriage.toml 后按需修改。
[general]
include_warnings = false     # 是否把 WARN 也纳入分析
max_groups = 20              # 报告里最多展示多少组错误
fail_on = "high"             # analyze 退出码阈值：none/low/medium/high/critical

[project]
root = ""                    # 接项目就填项目根目录，例如 "E:/work/project"
excludes = ["node_modules", "dist", "build", "target", ".venv"]
max_file_kb = 512

[llm]
enabled = true
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"   # 也可以直接写 api_key（不推荐提交到仓库）
timeout = 45
max_groups = 5               # 最多让 LLM 深挖几组，省钱
temperature = 0.2

[notify]
enabled = false              # 也可以不写这个，用命令行 --notify 临时开启
min_severity = "high"
cooldown_minutes = 30
subject_prefix = "[logtriage]"

# 控制台输出（默认）
[[notify.channels]]
kind = "console"
enabled = true

# 飞书群机器人：style 支持 generic / feishu / dingtalk / wecom
# [[notify.channels]]
# kind = "webhook"
# enabled = true
# url = "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
# style = "feishu"
# secret = ""                # 飞书/钉钉开启加签时填写
#
# # Telegram
# [[notify.channels]]
# kind = "telegram"
# enabled = true
# token_env = "TELEGRAM_BOT_TOKEN"
# chat_id = "123456789"
#
# # 邮件
# [[notify.channels]]
# kind = "email"
# enabled = true
# host = "smtp.example.com"
# port = 465
# ssl = true
# username = "alert@example.com"
# password_env = "SMTP_PASSWORD"
# from = "alert@example.com"
# to = ["oncall@example.com"]
"""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _table(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置项 {name} 必须是一个表（[section]）")
    return value


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass
class GeneralSettings:
    include_warnings: bool = False
    max_groups: int = 20
    fail_on: str = "high"
    language: str = "zh"
    extra_rules: list[str] = field(default_factory=list)


@dataclass
class ProjectSettings:
    root: str = ""
    excludes: list[str] = field(default_factory=list)
    max_file_kb: int = 512
    max_files: int = 8000


@dataclass
class LLMSettings:
    enabled: bool = True
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = ""
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout: float = 45.0
    max_groups: int = 5
    temperature: float = 0.2


@dataclass
class ChannelSettings:
    kind: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotifySettings:
    enabled: bool = False
    min_severity: str = "high"
    cooldown_minutes: float = 30.0
    subject_prefix: str = "[logtriage]"
    channels: list[ChannelSettings] = field(default_factory=list)


@dataclass
class Settings:
    general: GeneralSettings = field(default_factory=GeneralSettings)
    project: ProjectSettings = field(default_factory=ProjectSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    config_path: str | None = None

    def llm_api_key(self) -> str:
        return (self.llm.api_key or os.environ.get(self.llm.api_key_env, "")).strip()

    def llm_available(self) -> bool:
        return bool(self.llm.enabled and self.llm_api_key())


def locate_config(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"找不到配置文件：{path}")
        return path
    env_path = os.environ.get("LOGTRIAGE_CONFIG", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"LOGTRIAGE_CONFIG 指向的配置文件不存在：{path}")
        return path
    for name in ("logtriage.toml", ".logtriage.toml"):
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    return None


def load_settings(
    path: str | None = None,
    *,
    project: str | None = None,
    no_llm: bool = False,
) -> Settings:
    config_path = locate_config(path)
    raw: dict[str, Any] = {}
    if config_path is not None:
        try:
            raw = tomllib.loads(read_text_safe(config_path))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ConfigError(f"读取配置失败 {config_path}: {exc}") from exc

    merged = _deep_merge(DEFAULTS, raw)
    general_raw = _table(merged.get("general"), "general")
    project_raw = _table(merged.get("project"), "project")
    llm_raw = _table(merged.get("llm"), "llm")
    notify_raw = _table(merged.get("notify"), "notify")

    general = GeneralSettings(
        include_warnings=_bool(general_raw.get("include_warnings"), False),
        max_groups=max(1, int(general_raw.get("max_groups", 20) or 20)),
        fail_on=str(general_raw.get("fail_on", "high") or "high").lower(),
        language=str(general_raw.get("language", "zh") or "zh"),
        extra_rules=[str(item) for item in (general_raw.get("extra_rules") or [])],
    )
    if general.fail_on not in FAIL_ON_CHOICES:
        raise ConfigError(f"general.fail_on 必须是 {FAIL_ON_CHOICES} 之一，当前为 {general.fail_on!r}")

    project_settings = ProjectSettings(
        root=str(project_raw.get("root", "") or ""),
        excludes=[str(item) for item in (project_raw.get("excludes") or [])],
        max_file_kb=max(16, int(project_raw.get("max_file_kb", 512) or 512)),
        max_files=max(1, int(project_raw.get("max_files", 8000) or 8000)),
    )
    if project:
        project_settings.root = project

    llm = LLMSettings(
        enabled=_bool(llm_raw.get("enabled"), True),
        base_url=str(llm_raw.get("base_url", "https://api.deepseek.com") or "").rstrip("/"),
        model=str(llm_raw.get("model", "deepseek-chat") or "deepseek-chat"),
        api_key=str(llm_raw.get("api_key", "") or ""),
        api_key_env=str(llm_raw.get("api_key_env", "DEEPSEEK_API_KEY") or "DEEPSEEK_API_KEY"),
        timeout=float(llm_raw.get("timeout", 45) or 45),
        max_groups=max(0, int(llm_raw.get("max_groups", 5) or 0)),
        temperature=float(llm_raw.get("temperature", 0.2) or 0.0),
    )
    if no_llm:
        llm.enabled = False
    if llm.timeout < 5:
        llm.timeout = 5.0

    channels: list[ChannelSettings] = []
    raw_channels = notify_raw.get("channels") or []
    if not isinstance(raw_channels, list):
        raise ConfigError("notify.channels 必须是 [[notify.channels]] 数组")
    for item in raw_channels:
        if not isinstance(item, dict) or not item.get("kind"):
            raise ConfigError("每个 notify.channels 条目都必须带 kind")
        kind = str(item["kind"]).lower()
        options = {key: value for key, value in item.items() if key not in {"kind", "enabled"}}
        channels.append(ChannelSettings(kind=kind, enabled=_bool(item.get("enabled"), True), options=options))

    min_severity = str(notify_raw.get("min_severity", "high") or "high").lower()
    if min_severity not in SEVERITY_CHOICES:
        raise ConfigError(f"notify.min_severity 必须是 {SEVERITY_CHOICES} 之一")

    notify = NotifySettings(
        enabled=_bool(notify_raw.get("enabled"), False),
        min_severity=min_severity,
        cooldown_minutes=max(0.0, float(notify_raw.get("cooldown_minutes", 30) or 0)),
        subject_prefix=str(notify_raw.get("subject_prefix", "[logtriage]") or "[logtriage]"),
        channels=channels,
    )

    return Settings(
        general=general,
        project=project_settings,
        llm=llm,
        notify=notify,
        config_path=str(config_path) if config_path else None,
    )
