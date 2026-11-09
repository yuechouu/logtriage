"""logtriage —— 日志分析智能体。

它做四件事：
1. 从日志里识别报错（多行堆栈、JSON 日志、访问日志都能吃）；
2. 把同类报错聚合成一个「错误组」，避免刷屏；
3. 可选接入项目源码：顺着堆栈/日志文案定位到具体文件和行，并做根因推断；
4. 生成 Markdown/JSON 报告，或把摘要推送给飞书/钉钉/企业微信/Telegram/邮件/任意 Webhook。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .models import AnalysisResult, CodeRef, ErrorGroup, Frame, LogEvent

__all__ = [
    "__version__",
    "AnalysisResult",
    "CodeRef",
    "ErrorGroup",
    "Frame",
    "LogEvent",
]
