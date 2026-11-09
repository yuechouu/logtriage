from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from logtriage.models import ErrorGroup, Frame
from logtriage.project import ProjectIndex, extract_file_candidates


class ProjectIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "app").mkdir()
        (cls.root / ".venv").mkdir()
        (cls.root / "app" / "__init__.py").write_text("", encoding="utf-8")
        (cls.root / "app" / "orders.py").write_text(
            textwrap.dedent(
                """\
                \"\"\"Order helpers.\"\"\"

                import sqlite3


                def calculate_total(item):
                    quantity = int(item["quantity"])
                    price = float(item["price"])
                    return quantity * price


                def load_order(order_id):
                    connection = sqlite3.connect("orders.db")
                    return connection.execute("select * from orders where id = ?", (order_id,))
                """
            ),
            encoding="utf-8",
        )
        (cls.root / ".venv" / "ignored.py").write_text("def ignored():\n    pass\n", encoding="utf-8")
        cls.index = ProjectIndex(cls.root).build()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_build_skips_excluded_dirs(self) -> None:
        self.assertIn("app/orders.py", self.index.files)
        self.assertNotIn(".venv/ignored.py", self.index.files)
        self.assertFalse(self.index.warnings)

    def test_resolve_absolute_and_basename(self) -> None:
        self.assertEqual(self.index.resolve_path("/srv/demo/app/orders.py"), "app/orders.py")
        self.assertEqual(self.index.resolve_path("app/orders.py"), "app/orders.py")
        self.assertEqual(self.index.resolve_path("orders.py"), "app/orders.py")
        self.assertIsNone(self.index.resolve_path("missing/nope.py"))

    def test_search_frame_returns_stack_location_and_definition(self) -> None:
        refs = self.index.search_frame(Frame(file="/srv/demo/app/orders.py", line=7, function="calculate_total"))
        self.assertEqual(refs[0].path, "app/orders.py")
        self.assertEqual(refs[0].line, 7)
        self.assertEqual(refs[0].kind, "stack")
        self.assertTrue(any("quantity" in line for line in refs[0].snippet))
        self.assertTrue(any(ref.kind == "symbol" for ref in refs))

    def test_search_symbol(self) -> None:
        refs = self.index.search_symbol("load_order")
        self.assertTrue(refs)
        self.assertEqual(refs[0].path, "app/orders.py")
        self.assertEqual(refs[0].kind, "symbol")

    def test_references_for_group_uses_message_file(self) -> None:
        group = ErrorGroup(
            fingerprint="abc",
            title="KeyError: 'quantity'",
            exception_type="KeyError",
            sample_message="failed to calculate total",
            count=1,
            stack=[Frame(file="/srv/demo/app/orders.py", line=7, function="calculate_total")],
        )
        refs = self.index.references_for_group(group)
        self.assertTrue(refs)
        self.assertEqual(refs[0].path, "app/orders.py")
        self.assertEqual(refs[0].line, 7)
        self.assertEqual(refs[0].kind, "stack")

    def test_keyword_fallback_only_for_exception_groups(self) -> None:
        plain = ErrorGroup(fingerprint="p", title="GET /x 500", exception_type=None, sample_message="GET /x 500", count=1)
        self.assertEqual(self.index.references_for_group(plain), [])

    def test_extract_file_candidates(self) -> None:
        text = (
            'File "/srv/app/orders.py", line 22, in calculate_total\n'
            "java.lang.NullPointerException at C:\\work\\OrderService.java:15\n"
            "see worker/tasks.py:24"
        )
        found = extract_file_candidates(text)
        self.assertIn(("/srv/app/orders.py", 22), found)
        self.assertTrue(any(path.endswith("OrderService.java") and line == 15 for path, line in found))
        self.assertTrue(any(path.endswith("worker/tasks.py") and line == 24 for path, line in found))


if __name__ == "__main__":
    unittest.main()
