from __future__ import annotations

import unittest
from datetime import datetime

from logtriage.models import Frame, LogEvent
from logtriage.signature import fingerprint_for, group_events, normalize_message


def make_event(message: str, exception: str, order_id: int = 1) -> LogEvent:
    return LogEvent(
        index=order_id,
        raw=f"ERROR {message}",
        message=message,
        level="ERROR",
        timestamp=datetime(2026, 11, 9, 10, order_id % 60),
        exception_type=exception,
        exception_message="'quantity'",
        is_error=True,
    )


class SignatureTests(unittest.TestCase):
    def test_normalize_replaces_variable_parts(self) -> None:
        one = normalize_message("order 42 failed for 10.0.0.1 at 2026-11-09T10:00:00Z id=abc123")
        two = normalize_message("order 77 failed for 10.0.0.2 at 2026-11-10T11:22:33Z id=def456")
        self.assertEqual(one, two)
        self.assertIn("<n>", one)
        self.assertNotIn("42", one)

    def test_group_merges_same_error(self) -> None:
        events = [
            make_event("failed to calculate order 42", "KeyError", 1),
            make_event("failed to calculate order 77", "KeyError", 2),
            make_event("payment failed", "TypeError", 3),
        ]
        groups = group_events(events)
        self.assertEqual(len(groups), 2)
        key_group = next(group for group in groups if group.exception_type == "KeyError")
        self.assertEqual(key_group.count, 2)
        self.assertEqual(key_group.severity, "high")
        self.assertEqual(key_group.first_seen, events[0].timestamp)
        self.assertEqual(key_group.last_seen, events[1].timestamp)

    def test_fingerprint_is_stable_and_short(self) -> None:
        event = make_event("order 42 failed", "KeyError", 1)
        one = fingerprint_for(event)
        event.index = 2
        event.message = "order 77 failed"
        event.raw = "ERROR order 77 failed"
        two = fingerprint_for(event)
        self.assertEqual(one, two)
        self.assertEqual(len(one), 12)

    def test_stack_frames_are_kept(self) -> None:
        event = make_event("failed", "KeyError", 1)
        event.stack = [Frame(file="/app/a.py", line=2, function="f"), Frame(file="/app/a.py", line=9, function="g")]
        group = group_events([event])[0]
        self.assertEqual([frame.line for frame in group.stack], [2, 9])


if __name__ == "__main__":
    unittest.main()
