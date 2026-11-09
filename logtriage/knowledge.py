"""错误知识库：内置规则 + 用户自定义 JSON 规则。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from .models import ErrorGroup
from .utils import highest_severity, severity_rank

VALID_SEVERITIES = {"low", "medium", "high", "critical"}
FIELD_WEIGHTS = {"exception": 4, "http_status": 4, "message": 3, "raw": 2, "level": 1}
MATCH_FIELDS = set(FIELD_WEIGHTS)


@dataclass
class Rule:
    id: str
    title: str
    severity: str = "medium"
    tags: list[str] = field(default_factory=list)
    cause: str = ""
    fixes: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict, repr=False)
    source: str = "builtin"

    def match_details(self, group: ErrorGroup) -> tuple[int, list[str]] | None:
        """所有给出的条件都命中才算匹配；返回权重分和命中的字段。"""
        if not self.checks:
            return None
        levels = {(event.level or "").upper() for event in group.events}
        statuses = {event.http_status for event in group.events if event.http_status}
        exception_text = group.exception_type or ""
        message_text = group.sample_message or group.title or ""
        raw_text = "\n".join(event.raw[:1500] for event in group.events[:5])

        matched: list[str] = []
        score = 0
        for field_name, check in self.checks.items():
            if field_name == "level":
                if check not in levels:
                    return None
            elif field_name == "http_status":
                expected = check if isinstance(check, (list, tuple, set)) else [check]
                if not any(status in statuses for status in expected):
                    return None
            else:
                text = {
                    "exception": exception_text,
                    "message": message_text,
                    "raw": raw_text,
                }[field_name]
                if not check.search(text):
                    return None
            matched.append(field_name)
            score += FIELD_WEIGHTS[field_name]
        return score, matched


class RuleSet:
    def __init__(self, rules: list[Rule] | None = None, warnings: list[str] | None = None) -> None:
        # 注意：不能用 `rules or []`，否则空列表会被替换成新对象，
        # load() 后续 append 到局部列表时 rule_set.rules 永远是空的。
        self.rules = rules if rules is not None else []
        self.warnings = warnings if warnings is not None else []
        self.builtin_count = 0
        self.custom_count = 0

    @classmethod
    def load(cls, paths: Iterable[str | Path] | None = None, include_builtin: bool = True) -> "RuleSet":
        rules: list[Rule] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()
        rule_set = cls(rules, warnings)

        if include_builtin:
            bundled = cls._load_bundled(warnings)
            for raw in bundled:
                rule = cls._build_rule(raw, "builtin", warnings)
                if rule and rule.id not in seen_ids:
                    seen_ids.add(rule.id)
                    rules.append(rule)
                    rule_set.builtin_count += 1

        for path in paths or []:
            candidate = Path(path).expanduser()
            files = sorted(candidate.glob("*.json")) if candidate.is_dir() else [candidate]
            for file in files:
                if not file.is_file():
                    warnings.append(f"规则文件不存在：{file}")
                    continue
                try:
                    data = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    warnings.append(f"规则文件读取失败 {file}：{exc}")
                    continue
                raw_rules = data.get("rules") if isinstance(data, dict) else data
                if not isinstance(raw_rules, list):
                    warnings.append(f"规则文件格式不对（需要 rules 数组）：{file}")
                    continue
                for raw in raw_rules:
                    rule = cls._build_rule(raw, f"custom:{file.name}", warnings)
                    if not rule:
                        continue
                    if rule.id in seen_ids:
                        warnings.append(f"规则 id 重复，已跳过：{rule.id}（{file.name}）")
                        continue
                    seen_ids.add(rule.id)
                    rules.append(rule)
                    rule_set.custom_count += 1
        return rule_set

    @staticmethod
    def _load_bundled(warnings: list[str]) -> list[dict[str, Any]]:
        try:
            target = resources.files("logtriage").joinpath("data/error_rules.json")
            text = target.read_text(encoding="utf-8")
            data = json.loads(text)
        except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError) as exc:
            warnings.append(f"内置错误知识库加载失败：{exc}")
            return []
        rules = data.get("rules") if isinstance(data, dict) else data
        if not isinstance(rules, list):
            warnings.append("内置错误知识库格式不对")
            return []
        return rules

    @staticmethod
    def _build_rule(raw: Any, source: str, warnings: list[str]) -> Rule | None:
        if not isinstance(raw, dict):
            warnings.append(f"跳过非对象规则（{source}）")
            return None
        rule_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not rule_id or not title:
            warnings.append(f"规则缺少 id/title，已跳过（{source}）")
            return None
        match_raw = raw.get("match")
        if not isinstance(match_raw, dict) or not match_raw:
            warnings.append(f"规则 {rule_id} 缺少 match，已跳过")
            return None
        checks: dict[str, Any] = {}
        for field_name, value in match_raw.items():
            key = str(field_name).lower()
            if key not in MATCH_FIELDS:
                warnings.append(f"规则 {rule_id} 含未知匹配字段 {field_name}，已跳过")
                return None
            if value in (None, ""):
                continue
            elif key == "http_status":
                values = value if isinstance(value, (list, tuple, set)) else [value]
                statuses: list[int] = []
                try:
                    statuses = [int(item) for item in values]
                except (TypeError, ValueError):
                    warnings.append(f"规则 {rule_id} 的 http_status 不是整数")
                    return None
                if not statuses:
                    continue
                checks[key] = statuses
            elif key == "level":
                checks[key] = str(value).strip().upper()
            else:
                try:
                    checks[key] = re.compile(str(value), re.I)
                except re.error as exc:
                    warnings.append(f"规则 {rule_id} 的正则无效：{exc}")
                    return None
        if not checks:
            warnings.append(f"规则 {rule_id} 没有有效匹配条件，已跳过")
            return None

        severity = str(raw.get("severity") or "medium").lower()
        if severity not in VALID_SEVERITIES:
            warnings.append(f"规则 {rule_id} 的 severity={severity} 非法，按 medium 处理")
            severity = "medium"
        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        fixes = raw.get("fixes") if isinstance(raw.get("fixes"), list) else []
        return Rule(
            id=rule_id,
            title=title,
            severity=severity,
            tags=[str(item) for item in tags if str(item).strip()],
            cause=str(raw.get("cause") or "").strip(),
            fixes=[str(item).strip() for item in fixes if str(item).strip()],
            checks=checks,
            source=source,
        )

    def match(self, group: ErrorGroup) -> tuple[Rule, int, list[str]] | None:
        best: tuple[Rule, int, list[str]] | None = None
        best_key: tuple[int, int, int] | None = None
        for rule in self.rules:
            details = rule.match_details(group)
            if details is None:
                continue
            score, fields = details
            key = (score, severity_rank(rule.severity), -len(rule.id))
            if best_key is None or key > best_key:
                best_key = key
                best = (rule, score, fields)
        return best

    def apply(self, group: ErrorGroup) -> Rule | None:
        hit = self.match(group)
        if hit is None:
            return None
        rule, score, _fields = hit
        group.rule_id = rule.id
        group.rule_title = rule.title
        if rule.cause and not group.root_cause:
            group.root_cause = rule.cause
        if rule.fixes:
            group.fixes = list(dict.fromkeys(rule.fixes + group.fixes))
        group.severity = highest_severity(group.severity, rule.severity)
        group.confidence = max(group.confidence, min(0.8, 0.55 + 0.04 * score))
        if group.analyzer == "heuristic":
            group.analyzer = "rules"
        return rule

    def describe(self) -> list[dict[str, str]]:
        return [
            {
                "id": rule.id,
                "title": rule.title,
                "severity": rule.severity,
                "source": rule.source,
                "tags": "、".join(rule.tags),
            }
            for rule in self.rules
        ]
