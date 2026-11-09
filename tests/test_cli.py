from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_LOG = ROOT / "examples" / "demo.log"
DEMO_PROJECT = ROOT / "examples" / "demo_project"


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "logtriage", *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )


class CliTests(unittest.TestCase):
    def test_rules_json(self) -> None:
        result = run_cli("rules", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = json.loads(result.stdout)
        self.assertGreaterEqual(len(rules), 70)
        self.assertTrue(all("id" in rule and "title" in rule for rule in rules))

    def test_analyze_json_and_fail_on(self) -> None:
        result = run_cli(
            "analyze",
            str(DEMO_LOG),
            "--project",
            str(DEMO_PROJECT),
            "--no-llm",
            "-f",
            "json",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        self.assertGreaterEqual(len(data["groups"]), 8)
        self.assertEqual(data["error_events"], 12)

    def test_analyze_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.md"
            result = run_cli(
                "analyze",
                str(DEMO_LOG),
                "--project",
                str(DEMO_PROJECT),
                "--no-llm",
                "-f",
                "markdown",
                "-o",
                str(output),
            )
            self.assertIn(result.returncode, (0, 1), result.stderr)
            self.assertTrue(output.exists())
            self.assertIn("KeyError", output.read_text(encoding="utf-8"))

    def test_init_and_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp)
            init = run_cli("init", cwd=cwd)
            self.assertEqual(init.returncode, 0, init.stderr)
            config = cwd / "logtriage.toml"
            self.assertTrue(config.exists())
            self.assertIn("[llm]", config.read_text(encoding="utf-8"))

            second = run_cli("init", cwd=cwd)
            self.assertEqual(second.returncode, 2)

    def test_check_returns_exit_code(self) -> None:
        result = run_cli(
            "check",
            str(DEMO_LOG),
            "--project",
            str(DEMO_PROJECT),
            "--no-llm",
            "--fail-on",
            "high",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(result.stdout.strip())

    def test_invalid_path_exits_2(self) -> None:
        result = run_cli("analyze", str(ROOT / "nope-does-not-exist.log"), "--no-llm")
        self.assertEqual(result.returncode, 2)
        self.assertIn("不存在", result.stderr)


if __name__ == "__main__":
    unittest.main()
