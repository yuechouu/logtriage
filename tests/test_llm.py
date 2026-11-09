from __future__ import annotations

import unittest

from logtriage.config import LLMSettings
from logtriage.llm import LLMClient, LLMError, _extract_json, _normalize_analysis


class LLMHelperTests(unittest.TestCase):
    def test_extract_json_strips_code_fence(self) -> None:
        data = _extract_json('```json\n{"root_cause": "数据库连接池耗尽"}\n```')
        self.assertEqual(data["root_cause"], "数据库连接池耗尽")

    def test_extract_json_finds_embedded_object(self) -> None:
        data = _extract_json('分析如下：{"root_cause": "x"} 完毕')
        self.assertEqual(data, {"root_cause": "x"})

    def test_extract_json_rejects_plain_text(self) -> None:
        with self.assertRaises(LLMError):
            _extract_json("没有 JSON")

    def test_normalize_analysis(self) -> None:
        data = _normalize_analysis(
            {
                "root_cause": " 缺少字段 ",
                "analysis": "上游未返回 quantity。",
                "fixes": ["校验入参", "  ", 3],
                "severity": "HIGH",
                "confidence": 1.7,
                "related_files": "app/orders.py:22",
            }
        )
        self.assertEqual(data["root_cause"], "缺少字段")
        self.assertEqual(data["fixes"], ["校验入参", "3"])
        self.assertEqual(data["severity"], "high")
        self.assertEqual(data["confidence"], 1.0)
        self.assertEqual(data["related_files"], ["app/orders.py:22"])

    def test_invalid_severity_is_blank(self) -> None:
        data = _normalize_analysis({"root_cause": "x", "severity": "urgent"})
        self.assertEqual(data["severity"], "")

    def test_client_availability_and_endpoint(self) -> None:
        settings = LLMSettings(base_url="https://api.deepseek.com/", api_key="sk-test")
        client = LLMClient(settings)
        self.assertTrue(client.available)
        self.assertEqual(client._endpoint(), "https://api.deepseek.com/chat/completions")

        settings = LLMSettings(base_url="https://example.com/v1", api_key="")
        client = LLMClient(settings)
        self.assertFalse(client.available)
        self.assertEqual(client._endpoint(), "https://example.com/v1/chat/completions")

    def test_unavailable_client_raises(self) -> None:
        client = LLMClient(LLMSettings(api_key=""))
        with self.assertRaises(LLMError):
            client._request([{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()
