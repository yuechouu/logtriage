from __future__ import annotations

import unittest
from pathlib import Path

from logtriage.analyze import analyze_paths
from logtriage.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


class DemoAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        settings = load_settings(project=str(ROOT / "examples" / "demo_project"), no_llm=True)
        cls.result = analyze_paths([str(ROOT / "examples" / "demo.log")], settings, use_llm=False)

    def test_counts_and_project(self) -> None:
        self.assertGreaterEqual(self.result.total_events, 15)
        self.assertGreaterEqual(self.result.error_events, 10)
        self.assertGreaterEqual(len(self.result.groups), 8)
        self.assertTrue(self.result.project)
        self.assertTrue(self.result.project.endswith("demo_project"))
        self.assertFalse(self.result.llm_used)

    def test_keyerror_points_to_orders_line_23(self) -> None:
        group = next(group for group in self.result.groups if group.exception_type == "KeyError")
        self.assertEqual(group.count, 3)
        self.assertTrue(group.code_refs)
        self.assertEqual(group.code_refs[0].path, "app/orders.py")
        self.assertEqual(group.code_refs[0].line, 23)
        self.assertEqual(group.code_refs[0].kind, "stack")

    def test_module_not_found_points_to_worker_line_24(self) -> None:
        group = next(group for group in self.result.groups if group.exception_type == "ModuleNotFoundError")
        paths = [(ref.path, ref.line) for ref in group.code_refs]
        self.assertIn(("worker/tasks.py", 24), paths)

    def test_rule_annotations_present(self) -> None:
        key_group = next(group for group in self.result.groups if group.exception_type == "KeyError")
        self.assertTrue(key_group.rule_id)
        self.assertTrue(key_group.root_cause)
        self.assertTrue(key_group.fixes)

    def test_every_group_has_title_and_recent_time(self) -> None:
        for group in self.result.groups:
            self.assertTrue(group.title)
            self.assertIsNotNone(group.last_seen)
            self.assertGreater(group.count, 0)


if __name__ == "__main__":
    unittest.main()
