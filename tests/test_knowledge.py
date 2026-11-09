from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from logtriage.knowledge import RuleSet
from logtriage.models import LogEvent
from logtriage.signature import group_events


def make_group(exception: str, message: str = "something failed"):
    event = LogEvent(
        index=0,
        raw=f"ERROR app - {message}",
        message=message,
        level="ERROR",
        exception_type=exception,
        exception_message="boom",
        is_error=True,
    )
    return group_events([event])[0]


class KnowledgeTests(unittest.TestCase):
    def test_builtin_rules_load(self) -> None:
        ruleset = RuleSet.load()
        self.assertGreaterEqual(len(ruleset.rules), 70)
        ids = [rule.id for rule in ruleset.rules]
        self.assertEqual(len(ids), len(set(ids)))

    def test_keyerror_rule_applies(self) -> None:
        ruleset = RuleSet.load()
        group = make_group("KeyError")
        ruleset.apply(group)
        self.assertTrue(group.rule_id)
        self.assertTrue(group.rule_title)
        self.assertTrue(group.root_cause)
        self.assertTrue(group.fixes)
        self.assertEqual(group.analyzer, "rules")
        self.assertGreaterEqual(group.confidence, 0.55)

    def test_unknown_exception_keeps_heuristic(self) -> None:
        ruleset = RuleSet.load()
        group = make_group("TotallyCustomBoomError")
        ruleset.apply(group)
        self.assertIsNone(group.rule_id)
        self.assertEqual(group.analyzer, "heuristic")

    def test_custom_rule_file(self) -> None:
        payload = {
            "version": 1,
            "rules": [
                {
                    "id": "custom-boom",
                    "title": "自定义 Boom",
                    "severity": "critical",
                    "tags": ["custom"],
                    "match": {"exception": "^CustomBoomError$"},
                    "cause": "演示用的自定义异常。",
                    "fixes": ["检查配置", "重试一次"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rules.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            ruleset = RuleSet.load([str(path)])
        group = make_group("CustomBoomError")
        ruleset.apply(group)
        self.assertEqual(group.rule_id, "custom-boom")
        self.assertEqual(group.severity, "critical")
        self.assertEqual(group.fixes, ["检查配置", "重试一次"])

    def test_rule_matches_require_all_fields(self) -> None:
        ruleset = RuleSet.load()
        group = make_group("KeyError")
        group.sample_message = "completely unrelated text"
        ruleset.apply(group)
        # 内置 KeyError 规则只约束 exception，应仍然命中。
        self.assertTrue(group.rule_id)


if __name__ == "__main__":
    unittest.main()
