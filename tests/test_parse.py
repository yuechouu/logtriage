from __future__ import annotations

import unittest
from datetime import datetime, timezone

from logtriage.parse import parse_text


class ParseBasicTests(unittest.TestCase):
    def test_plain_line(self) -> None:
        events = parse_text(
            "2026-11-09 10:00:01 ERROR app.orders - failed to calculate total",
            "app.log",
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.level, "ERROR")
        self.assertEqual(event.timestamp, datetime(2026, 11, 9, 10, 0, 1))
        self.assertEqual(event.service, "app")
        self.assertEqual(event.logger, "app.orders")
        self.assertIn("failed to calculate total", event.message)

    def test_python_traceback_is_one_event(self) -> None:
        text = """2026-11-09 10:01:12 ERROR [order-api] app.orders - failed to calculate total
Traceback (most recent call last):
  File "/srv/demo/app/orders.py", line 22, in calculate_total
    quantity = int(item["quantity"])
KeyError: 'quantity'
"""
        events = parse_text(text, "app.log")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.exception_type, "KeyError")
        self.assertEqual(event.exception_message, "'quantity'")
        self.assertEqual(len(event.stack), 1)
        self.assertEqual(event.stack[0].file, "/srv/demo/app/orders.py")
        self.assertEqual(event.stack[0].line, 22)
        self.assertEqual(event.stack[0].function, "calculate_total")

    def test_bare_exception_line_joins_previous_log_line(self) -> None:
        text = """2026-11-09 10:04:10 ERROR [billing] redis - cache connection lost
RedisConnectionError: Error 111 connecting to cache:6379. Connection refused.
2026-11-09 10:05:00 INFO [billing] redis - retry
"""
        events = parse_text(text, "app.log")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].exception_type, "RedisConnectionError")
        self.assertEqual(events[0].level, "ERROR")
        self.assertEqual(events[0].timestamp, datetime(2026, 11, 9, 10, 4, 10))
        self.assertEqual(events[1].level, "INFO")

    def test_access_log_timezone(self) -> None:
        line = (
            '127.0.0.1 - - [09/Nov/2026:10:07:01 +0800] "GET /api/orders/42 HTTP/1.1" '
            '500 123 "-" "curl/8.0"'
        )
        event = parse_text(line, "access.log")[0]
        expected = datetime(2026, 11, 9, 2, 7, 1, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(event.timestamp, expected)
        self.assertEqual(event.http_status, 500)
        self.assertIn("500", event.message)

    def test_json_event(self) -> None:
        line = (
            '{"ts": "2026-11-09T10:08:00+08:00", "level": "ERROR", "service": "worker", '
            '"message": "job failed", "exception": "ModuleNotFoundError: No module named \'redis\'", '
            '"stack": "Traceback (most recent call last):\\n  File \\"/srv/demo/worker/tasks.py\\", '
            'line 24, in run\\n    import redis\\nModuleNotFoundError: No module named \'redis\'"}'
        )
        events = parse_text(line, "worker.jsonl")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.level, "ERROR")
        self.assertEqual(event.service, "worker")
        self.assertEqual(event.timestamp, datetime(2026, 11, 9, 10, 8))
        self.assertEqual(event.exception_type, "ModuleNotFoundError")
        self.assertEqual(len(event.stack), 1)
        self.assertEqual(event.stack[0].line, 24)

    def test_blank_lines_do_not_drop_stack(self) -> None:
        text = """ERROR app - boom
Traceback (most recent call last):

  File "/app/x.py", line 3, in f
    raise ValueError("bad")
ValueError: bad
"""
        events = parse_text(text, "app.log")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].exception_type, "ValueError")


if __name__ == "__main__":
    unittest.main()
