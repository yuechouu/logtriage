from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from logtriage.config import ChannelSettings, NotifySettings
from logtriage.models import AnalysisResult, ErrorGroup
from logtriage.notify import notify


def result_with(severity: str, fingerprint: str = "abc") -> AnalysisResult:
    group = ErrorGroup(
        fingerprint=fingerprint,
        title="KeyError: 'quantity'",
        exception_type="KeyError",
        sample_message="failed",
        count=2,
        severity=severity,
        first_seen=datetime(2026, 11, 9, 10, 0, 0),
        last_seen=datetime(2026, 11, 9, 10, 1, 0),
    )
    return AnalysisResult(sources=["demo.log"], total_events=2, error_events=2, groups=[group])


class NotifyTests(unittest.TestCase):
    def test_dry_run_does_not_write_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            settings = NotifySettings(
                enabled=True,
                min_severity="high",
                channels=[ChannelSettings("console")],
            )
            results = notify(result_with("high"), settings, state_path=state, dry_run=True)
            self.assertEqual(results[0].kind, "dry-run")
            self.assertTrue(results[0].ok)
            self.assertFalse(state.exists())

    def test_console_sends_then_cooldown_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            settings = NotifySettings(
                enabled=True,
                min_severity="high",
                cooldown_minutes=60,
                channels=[ChannelSettings("console")],
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                first = notify(result_with("high"), settings, state_path=state)
            self.assertTrue(first[0].ok)
            self.assertIn("KeyError", buffer.getvalue())
            self.assertTrue(state.exists())
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertIn("abc", saved["notified"])

            second = notify(result_with("high"), settings, state_path=state)
            self.assertFalse(second[0].ok)
            self.assertIn("冷却", second[0].detail)

    def test_below_threshold_is_not_sent(self) -> None:
        settings = NotifySettings(enabled=True, min_severity="high", channels=[ChannelSettings("console")])
        with tempfile.TemporaryDirectory() as temp:
            results = notify(result_with("medium"), settings, state_path=Path(temp) / "s.json")
        self.assertFalse(results[0].ok)
        self.assertIn("没有达到", results[0].detail)

    def test_disabled_returns_nothing(self) -> None:
        settings = NotifySettings(enabled=False)
        self.assertEqual(notify(result_with("high"), settings), [])


if __name__ == "__main__":
    unittest.main()
