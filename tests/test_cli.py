"""End-to-end CLI behaviour, on a synthetic temporary fixture only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from hx import _git
from tests import support as S

SRC = Path(__file__).resolve().parents[1] / "src"


class CliTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()
        self.agent = self.sb.script("fix.py", S.AGENT_FIX_AND_NEWFILE)

    def hx(self, *args: str) -> subprocess.CompletedProcess:
        # A deliberately small environment: no ambient secrets, but git and the
        # package must still be reachable.
        path_value = os.pathsep.join(
            [
                str(SRC),
                *[p for p in sys.path if p and Path(p).is_dir()],
                os.environ.get("PATH", ""),
            ]
        )
        return subprocess.run(
            [sys.executable, "-m", "hx", *args],
            cwd=str(SRC.parent),
            capture_output=True,
            text=True,
            errors="replace",
            env={
                "PATH": path_value,
                "PYTHONPATH": str(SRC),
                "SYSTEMROOT": os.environ.get("SystemRoot", os.environ.get("SYSTEMROOT", "")),
                "HOME": str(self.sb.root),
                "USERPROFILE": str(self.sb.root),
                "TMP": str(self.sb.root),
                "TEMP": str(self.sb.root),
                "GIT_TERMINAL_PROMPT": "0",
            },
            timeout=300,
        )

    def test_run_inspect_diff_accept_flow(self) -> None:
        run = self.hx(
            "run",
            str(self.repo),
            "--runs-root",
            str(self.runs),
            "--timeout",
            "180",
            "--",
            sys.executable,
            str(self.agent),
            "--",
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-t",
            ".",
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("status=ok", run.stdout)
        run_id = [ln for ln in run.stdout.splitlines() if ln.startswith("run_id=")][0]
        run_id = run_id.split("=", 1)[1].strip()

        inspect = self.hx("inspect", run_id, "--runs-root", str(self.runs), "--json")
        self.assertEqual(inspect.returncode, 0, inspect.stdout + inspect.stderr)
        record = json.loads(inspect.stdout)
        self.assertEqual(record["status"], "ok")
        self.assertIn("words.py", record["changed_files"])

        diff = self.hx("diff", run_id, "--runs-root", str(self.runs))
        self.assertEqual(diff.returncode, 0, diff.stdout + diff.stderr)
        self.assertIn("new file mode", diff.stdout)

        # accept needs --yes
        no_yes = self.hx("accept", run_id, "--runs-root", str(self.runs))
        self.assertNotEqual(no_yes.returncode, 0)
        self.assertIn("--yes", no_yes.stdout + no_yes.stderr)

        accepted = self.hx("accept", run_id, "--yes", "--runs-root", str(self.runs))
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("applied", accepted.stdout)
        self.assertTrue((self.repo / "RELEASE_NOTES.md").is_file())
        self.assertIn('return "-".join(w.lower()', (self.repo / "words.py").read_text())
        # still uncommitted
        _, porcelain, _ = _git.git(["status", "--porcelain"], cwd=self.repo)
        self.assertIn("M words.py", porcelain)

        again = self.hx("accept", run_id, "--yes", "--runs-root", str(self.runs))
        self.assertNotEqual(again.returncode, 0)

    def test_run_exits_nonzero_when_verification_fails(self) -> None:
        noop = self.sb.script("noop.py", S.AGENT_NOOP)
        proc = self.hx(
            "run",
            str(self.repo),
            "--runs-root",
            str(self.runs),
            "--",
            sys.executable,
            str(noop),
            "--",
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-t",
            ".",
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("status=verifier_failed", proc.stdout)

    def test_missing_separator_is_a_usage_error(self) -> None:
        proc = self.hx("run", str(self.repo), "--", sys.executable, "-c", "pass")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage", (proc.stdout + proc.stderr).lower())

    def test_version_and_help(self) -> None:
        self.assertIn("hx", self.hx("--version").stdout)
        self.assertIn("demo", self.hx("--help").stdout)


if __name__ == "__main__":
    unittest.main()
