"""hx run --preflight and --json: nothing runs on preflight, and the result is versioned."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import hx
from hx import _git
from hx.cli import main
from tests import support as S


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()
        self.marker = Path(self.sb.root) / "agent-ran.txt"
        self.agent = self.sb.script(
            "mark.py", f"from pathlib import Path\nPath({str(self.marker)!r}).write_text('x')\n"
        )

    def preflight(self, **overrides):
        args = {
            "repo": self.repo,
            "agent_argv": [S.PY, str(self.agent)],
            "verify_argv": S.VERIFY_UNITTEST,
            "runs_root": self.runs,
        }
        args.update(overrides)
        return hx.preflight_run(**args)

    def failed(self, report) -> set[str]:
        return {c["name"] for c in report["checks"] if not c["ok"]}

    def test_a_valid_setup_passes_and_nothing_runs(self) -> None:
        report = self.preflight()
        self.assertTrue(report["ok"], report)
        self.assertFalse(self.marker.exists())
        self.assertEqual(list(Path(self.runs).glob("*")), [])
        names = {c["name"] for c in report["checks"]}
        self.assertTrue(
            {
                "agent_argv",
                "verifier_argv",
                "timeout",
                "pass_env",
                "source_repo",
                "runs_root",
                "agent_executable",
                "verifier_executable",
                "git_version",
            }
            <= names
        )

    def test_a_dirty_source_fails(self) -> None:
        (Path(self.repo) / "stray.txt").write_text("x", encoding="utf-8")
        self.assertIn("source_repo", self.failed(self.preflight()))

    def test_a_missing_executable_fails(self) -> None:
        report = self.preflight(agent_argv=["definitely-not-a-program-hx"])
        self.assertEqual(self.failed(report), {"agent_executable"})

    def test_an_empty_verifier_fails(self) -> None:
        self.assertIn("verifier_argv", self.failed(self.preflight(verify_argv=[])))

    def test_a_bad_timeout_fails(self) -> None:
        self.assertIn("timeout", self.failed(self.preflight(timeout=-1)))

    def test_runs_root_inside_the_repo_fails(self) -> None:
        self.assertIn("runs_root", self.failed(self.preflight(runs_root=Path(self.repo) / "runs")))

    def test_an_old_git_fails_the_version_check(self) -> None:
        with patch.object(_git, "git_version", return_value=(2, 31)):
            self.assertEqual(self.failed(self.preflight()), {"git_version"})
        with patch.object(_git, "git_version", return_value=None):
            self.assertEqual(self.failed(self.preflight()), {"git_version"})

    def test_an_invalid_pass_env_name_fails(self) -> None:
        self.assertIn("pass_env", self.failed(self.preflight(pass_env=["bad name"])))

    def test_secrets_in_argv_are_redacted_in_the_report(self) -> None:
        report = self.preflight(agent_argv=[S.PY, str(self.agent), "--api-key=sk-secret-value"])
        self.assertNotIn("sk-secret-value", json.dumps(report))

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_cli_preflight_exit_codes(self) -> None:
        base = ["run", str(self.repo), "--runs-root", str(self.runs), "--preflight"]
        code, text = self.run_cli(*base, "--", S.PY, str(self.agent), "--", *S.VERIFY_UNITTEST)
        self.assertEqual(code, 0, text)
        self.assertIn("ok   source_repo", text)
        self.assertFalse(self.marker.exists())
        code, text = self.run_cli(*base, "--", "no-such-program-hx", "--", *S.VERIFY_UNITTEST)
        self.assertEqual(code, 1)
        self.assertIn("FAIL agent_executable", text)


class ResultJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()

    def run_json(self, agent_body: str) -> tuple[int, dict]:
        agent = self.sb.script("a.py", agent_body)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(
                [
                    "run",
                    str(self.repo),
                    "--runs-root",
                    str(self.runs),
                    "--json",
                    "--",
                    S.PY,
                    str(agent),
                    "--",
                    *S.VERIFY_UNITTEST,
                ]
            )
        return code, json.loads(out.getvalue())

    def test_a_passing_run_is_versioned_and_exits_zero(self) -> None:
        code, result = self.run_json(S.AGENT_FIX)
        self.assertEqual(code, 0)
        self.assertEqual(result["schema"], "hx.result/1")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["passed"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("words.py", result["changed_files"])
        self.assertEqual(len(result["patch_sha256"]), 64)
        self.assertEqual(result["verifier"]["exit_code"], 0)
        self.assertEqual(
            sorted(result),
            sorted(
                [
                    "schema",
                    "run_id",
                    "status",
                    "passed",
                    "exit_code",
                    "base_commit",
                    "changed_files",
                    "patch_sha256",
                    "patch_bytes",
                    "agent",
                    "verifier",
                    "run_dir",
                ]
            ),
        )

    def test_a_run_that_does_not_pass_exits_three(self) -> None:
        code, result = self.run_json(S.AGENT_NOOP)
        self.assertEqual(code, 3)
        self.assertFalse(result["passed"])
        self.assertEqual(result["exit_code"], 3)
        self.assertNotEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
