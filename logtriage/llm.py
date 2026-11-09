"""可选的 LLM 深度分析：走 OpenAI 兼容的 /chat/completions。

没有 API key 时整个模块不会被调用，纯规则分析照常工作。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from .config import LLMSettings
from .models import AnalysisResult, ErrorGroup
from .utils import isoformat, truncate

SYSTEM_PROMPT = (
    "你是一名资深 SRE / 后端工程师，擅长根据生产日志、堆栈和少量源码定位线上报错的根因。"
    "回答必须具体、可执行，不要泛泛而谈。只输出 JSON，不要输出 Markdown 代码块。"
)

VALID_SEVERITIES = {"low", "medium", "high", "critical"}


class LLMError(RuntimeError):
    """LLM 调用失败（网络、鉴权、返回格式等）。"""


class LLMClient:
    def __init__(self, settings: LLMSettings, api_key: str | None = None) -> None:
        self.settings = settings
        self.api_key = (api_key if api_key is not None else settings.api_key or os.environ.get(settings.api_key_env, "")).strip()
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.enabled and self.api_key)

    def _endpoint(self) -> str:
        base = self.settings.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _request(self, messages: list[dict[str, str]], max_tokens: int = 900) -> str:
        if not self.available:
            raise LLMError("未配置 LLM（缺少 api_key 或已禁用）")
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except OSError:
                pass
            raise LLMError(f"LLM 接口返回 HTTP {exc.code}：{truncate(body, 300)}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM 网络错误：{exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError("LLM 请求超时") from exc
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 返回格式异常：{truncate(raw, 300)}") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM 返回了空内容")
        return content

    def analyze_group(self, group: ErrorGroup) -> dict[str, Any]:
        prompt = self._build_group_prompt(group)
        content = self._request(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=900,
        )
        data = _extract_json(content)
        analysis = _normalize_analysis(data)
        if not analysis["root_cause"] and not analysis["analysis"]:
            raise LLMError("LLM 返回内容里没有根因分析")
        return analysis

    def summarize(self, result: AnalysisResult) -> str:
        lines = [
            f"日志事件 {result.total_events} 条，错误事件 {result.error_events} 条，错误组 {len(result.groups)} 个。",
        ]
        for index, group in enumerate(result.groups[:8], 1):
            lines.append(
                f"{index}. [{group.severity}] {group.title}（{group.count} 次）"
                + (f"；规则判断：{group.root_cause}" if group.root_cause else "")
            )
        prompt = (
            "请用 2~4 句中文总结这批日志反映的主要问题、最该先处理什么。不要输出 JSON，不要用 Markdown。\n\n"
            + "\n".join(lines)
        )
        content = self._request(
            [
                {"role": "system", "content": "你是一名值班 SRE，说话简洁、直接给优先级。"},
                {"role": "user", "content": prompt[:6000]},
            ],
            max_tokens=300,
        )
        return truncate(content.strip().strip("`"), 800)

    def _build_group_prompt(self, group: ErrorGroup) -> str:
        parts = [
            "请分析下面这组线上日志报错，给出根因、代码层面的解释和修复建议。",
            "",
            f"错误标题：{group.title}",
            f"异常类型：{group.exception_type or '（无）'}",
            f"出现次数：{group.count}",
            f"首次出现：{isoformat(group.first_seen) or '未知'}",
            f"最近出现：{isoformat(group.last_seen) or '未知'}",
            f"当前严重度：{group.severity}",
            f"已匹配错误知识库：{group.rule_title or '（无）'}",
            f"知识库给出的可能原因：{group.root_cause or '（无）'}",
            f"样例消息：{truncate(group.sample_message, 600)}",
        ]
        if group.stack:
            parts.append("")
            parts.append("堆栈（前 10 帧）：")
            for frame in group.stack[:10]:
                function = f" in {frame.function}" if frame.function else ""
                parts.append(f"- {frame.where()}{function}")
        if group.code_refs:
            parts.append("")
            parts.append("相关代码片段（来自项目索引）：")
            for ref in group.code_refs[:4]:
                parts.append(f"- {ref.label()}（{ref.note}）")
                for line in ref.snippet[:9]:
                    parts.append("  " + line)
        parts.extend(
            [
                "",
                "请严格输出如下 JSON（中文内容）：",
                "{",
                '  "root_cause": "一句话根因",',
                '  "analysis": "结合堆栈/代码的详细解释，2~5 句",',
                '  "fixes": ["可执行的修复建议1", "建议2"],',
                '  "severity": "low|medium|high|critical",',
                '  "confidence": 0.0,',
                '  "related_files": ["path:line"]',
                "}",
                "如果证据不足，root_cause 里要明确说“证据不足”，不要编造不存在的代码。",
            ]
        )
        return "\n".join(parts)[:7000]


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"LLM 没有返回 JSON：{truncate(text, 200)}")
    try:
        data = json.loads(value[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM 返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("LLM 返回的 JSON 不是对象")
    return data


def _normalize_analysis(data: dict[str, Any]) -> dict[str, Any]:
    root_cause = data.get("root_cause") or data.get("cause") or ""
    analysis = data.get("analysis") or data.get("code_analysis") or data.get("explanation") or ""
    fixes_raw = data.get("fixes") or data.get("suggestions") or []
    if isinstance(fixes_raw, str):
        fixes = [fixes_raw]
    elif isinstance(fixes_raw, list):
        fixes = [str(item).strip() for item in fixes_raw if str(item).strip()]
    else:
        fixes = []

    severity = str(data.get("severity") or "").lower()
    if severity not in VALID_SEVERITIES:
        severity = ""

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    related = data.get("related_files") or []
    if isinstance(related, str):
        related = [related]
    related_files = [str(item).strip() for item in related if str(item).strip()] if isinstance(related, list) else []

    return {
        "root_cause": str(root_cause).strip(),
        "analysis": str(analysis).strip(),
        "fixes": fixes[:8],
        "severity": severity,
        "confidence": confidence,
        "related_files": related_files[:8],
    }
