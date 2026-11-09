from __future__ import annotations

import unittest

from logtriage.detect import classify_event, extract_http_statuses
from logtriage.models import LogEvent


def classify(message: str, level: str | None = None, include_warnings: bool = False) -> LogEvent:
    event = LogEvent(index=0, raw=f"{level or ''} {message}".strip(), message=message, level=level)
    return classify_event(event, include_warnings=include_warnings)


class DetectTests(unittest.TestCase):
    def test_error_level_is_error(self) -> None:
        event = classify("boom happened", level="ERROR")
        self.assertTrue(event.is_error)
        self.assertFalse(event.is_warning)
        self.assertIn("level=ERROR", event.score_reasons)

    def test_warn_is_not_error_by_default(self) -> None:
        event = classify("disk space low", level="WARN")
        self.assertFalse(event.is_error)
        self.assertTrue(event.is_warning)

    def test_warn_can_be_included(self) -> None:
        event = classify("disk space low", level="WARN", include_warnings=True)
        self.assertTrue(event.is_error)

    def test_keyword_without_level(self) -> None:
        event = classify("java.lang.NullPointerException: user is null")
        self.assertTrue(event.is_error)
        self.assertEqual(event.exception_type, "java.lang.NullPointerException")

    def test_http_5xx_scores(self) -> None:
        event = classify('"GET /api/orders HTTP/1.1" 502 12')
        self.assertEqual(event.http_status, 502)
        self.assertGreaterEqual(event.score, 2)
        self.assertTrue(event.is_error)

    def test_extract_http_statuses(self) -> None:
        self.assertEqual(extract_http_statuses("status=503 request failed"), [503])
        self.assertEqual(extract_http_statuses("HTTP/1.1 404"), [404])


if __name__ == "__main__":
    unittest.main()
