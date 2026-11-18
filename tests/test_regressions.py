"""Regression tests for boundaries not exercised by the happy-path demo."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hx import HxError, _env, _git, run
from hx._core import _normalize_argv, _run_step, _tail
from hx._process import execute
from tests import support as S


class BoundaryTest(unittest.TestCase):
    def test_redact_before_truncation_and_mask_short_values(self):
        secret = "abc"
        self.assertNotIn(secret, _env.mask_values("abc def", [secret]))
        self.assertNotIn("bc", _tail("x" * 3999 + secret + "z" * 3999, [secret]))

    def test_empty_arguments_are_preserved(self):
        self.assertEqual(_normalize_argv(["program", "", "end"], "agent"), ["program", "", "end"])

    def test_git_redirection_variables_are_removed(self):
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": "wrong",
                "GIT_INDEX_FILE": "wrong",
                "GIT_WORK_TREE": "wrong",
                "GIT_OBJECT_DIRECTORY": "wrong",
            },
        ):
            env = _git.git_env()
        for name in (
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_WORK_TREE",
            "GIT_OBJECT_DIRECTORY",
        ):
            self.assertNotIn(name, env)

    def test_inside_runs_root_is_rejected_before_creation(self):
        sb = S.Sandbox()
        self.addCleanup(sb.cleanup)
        repo = sb.new_repo()
        target = repo / "must-not-exist"
        with self.assertRaisesRegex(HxError, "outside"):
            run(repo, [S.PY, "-c", "pass"], S.VERIFY_TRUE, runs_root=target)
        self.assertFalse(target.exists())

    def test_nested_source_is_rejected_before_any_run_artifact(self):
        """A subdirectory of a checkout is refused by comparing it with Git's toplevel."""
        sb = S.Sandbox()
        self.addCleanup(sb.cleanup)
        repo = sb.new_repo()
        runs = sb.runs_root()
        nested = repo / "tests"
        inside_runs = repo / "runs"
        before = S.snapshot(repo)

        with self.assertRaisesRegex(HxError, "subdirectory"):
            run(nested, [S.PY, "-c", "pass"], S.VERIFY_TRUE, runs_root=runs)
        self.assertEqual([p.name for p in runs.iterdir()], [])

        # the containment check used to trust the supplied path, so a runs_root
        # under the real repository root slipped through: still nothing created.
        with self.assertRaisesRegex(HxError, "subdirectory"):
            run(nested, [S.PY, "-c", "pass"], S.VERIFY_TRUE, runs_root=inside_runs)
        self.assertFalse(inside_runs.exists())
        self.assertEqual([p.name for p in runs.iterdir()], [])
        self.assertEqual(S.snapshot(repo), before)

        # the work tree root itself is still accepted, and so is a linked work tree
        res = run(repo, [S.PY, "-c", "pass"], S.VERIFY_TRUE, runs_root=runs)
        self.assertTrue(res.work_dir.is_dir())
        self.assertEqual([p.name for p in runs.iterdir()], [res.run_id])

        linked = sb.path("linked-worktree")
        _git.git(["worktree", "add", "--detach", str(linked), "HEAD"], cwd=repo)
        self.assertEqual(_git.work_tree_root(linked), linked.resolve())
        linked_run = run(linked, [S.PY, "-c", "pass"], S.VERIFY_TRUE, runs_root=runs)
        self.assertTrue(linked_run.work_dir.is_dir())
        self.assertEqual(len([p.name for p in runs.iterdir()]), 2)

    def test_timeout_kills_a_descendant_holding_output_pipes(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "escaped.txt"
            child = f"import time,pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('escaped')"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)"
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                execute(
                    [S.PY, "-c", parent],
                    cwd=Path(temporary),
                    env=dict(os.environ),
                    timeout=0.7,
                )
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(1.2)
            self.assertFalse(marker.exists())

    def test_known_values_are_redacted_from_recorded_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            step = _run_step(
                [S.PY, "-c", "pass", "abc"],
                Path(temporary),
                dict(os.environ),
                5,
                ["abc"],
            )
            self.assertNotIn("abc", str(step.to_dict()))

    def test_skipped_verifier_arguments_are_masked(self):
        sb = S.Sandbox()
        self.addCleanup(sb.cleanup)
        repo = sb.new_repo()
        with patch.dict(os.environ, {"HX_SAMPLE": "abc"}):
            result = run(
                repo,
                [S.PY, "-c", "raise SystemExit(1)"],
                [S.PY, "-c", "pass", "abc"],
                runs_root=sb.runs_root(),
                pass_env=["HX_SAMPLE"],
            )
        self.assertTrue(result.verifier.skipped)
        self.assertNotIn("abc", str(result.verifier.to_dict()))
