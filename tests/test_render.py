from __future__ import annotations

import json
import unittest
from datetime import datetime

from logtriage.models import AnalysisResult, CodeRef, ErrorGroup, LogEvent
from logtriage.render import render_json, render_markdown, render_summary, render_text


def sample_result() -> AnalysisResult:
    event = LogEvent(
        index=0,
        raw="2026-11-09 10:01:12 ERROR app.orders - failed to calculate total",
        message="failed to calculate total",
        level="ERROR",
        timestamp=datetime(2026, 11, 9, 10, 1, 12),
        exception_type="KeyError",
        exception_message="'quantity'",
        is_error=True,
    )
    group = ErrorGroup(
        fingerprint="abc123def456",
        title="KeyError: 'quantity'",
        exception_type="KeyError",
        sample_message="failed to calculate total",
        count=3,
        first_seen=datetime(2026, 11, 9, 10, 1, 12),
        last_seen=datetime(2026, 11, 9, 10, 3, 5),
        severity="high",
        events=[event],
        code_refs=[
            CodeRef(
                path="app/orders.py",
                line=23,
                kind="stack",
                symbol="calculate_total",
                snippet=["   23 |     quantity = int(item[\"quantity\"])"],
                score=9.0,
                note="堆栈位置",
            )
        ],
        rule_id="python-keyerror",
        rule_title="KeyError 字典键不存在",
        root_cause="订单数据缺少 quantity 字段。",
        fixes=["检查上游返回字段", "使用 item.get('quantity', 0)"],
        confidence=0.72,
        analyzer="rules+code",
    )
    return AnalysisResult(
        sources=["demo.log"],
        project="examples/demo_project",
        generated_at=datetime(2026, 11, 9, 12, 0, 0),
        total_lines=100,
        total_events=10,
        error_events=4,
        groups=[group],
        level_counts={"ERROR": 4, "INFO": 6},
        services={"order-api": 7},
        first_ts=datetime(2026, 11, 9, 10, 1, 12),
        last_ts=datetime(2026, 11, 9, 10, 3, 5),
        notes=["仅启用启发式分析"],
    )


class RenderTests(unittest.TestCase):
    def test_markdown_contains_key_sections(self) -> None:
        markdown = render_markdown(sample_result())
        for expected in [
            "日志分析",
            "KeyError: 'quantity'",
            "app/orders.py",
            "可能原因",
            "修复建议",
            "检查上游返回字段",
            "python-keyerror",
        ]:
            self.assertIn(expected, markdown)

    def test_text_is_compact(self) -> None:
        text = render_text(sample_result())
        self.assertIn("KeyError: 'quantity'", text)
        self.assertIn("app/orders.py:23", text)
        self.assertNotIn("| ---", text)

    def test_summary_single_line(self) -> None:
        summary = render_summary(sample_result())
        self.assertLessEqual(summary.count("\n"), 1)
        self.assertIn("logtriage:", summary)
        self.assertIn("high", summary.lower())
        self.assertIn("KeyError", summary)

    def test_json_roundtrip(self) -> None:
        data = json.loads(render_json(sample_result()))
        self.assertEqual(data["project"], "examples/demo_project")
        self.assertEqual(data["groups"][0]["code_refs"][0]["line"], 23)


if __name__ == "__main__":
    unittest.main()
