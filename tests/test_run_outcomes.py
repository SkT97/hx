"""What a run measures and records. Synthetic temporary fixtures only."""

from __future__ import annotations

import hashlib
import unittest

import hx
from tests import support as S


class RunOutcomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()
        self.assertTrue(
            S.verifier_output_is_failing(self.repo),
            "fixture must fail its verifier before the agent touches it",
        )

    def do_run(self, agent_script: str, verify=None, timeout=180):
        return hx.run(
            self.repo,
            [S.PY, str(self.sb.script("a.py", agent_script))],
            verify if verify is not None else S.VERIFY_UNITTEST,
            timeout=timeout,
            runs_root=self.runs,
        )

    # --- happy path -------------------------------------------------------
    def test_success_records_outcome_and_leaves_source_untouched(self) -> None:
        before = S.snapshot(self.repo)
        res = self.do_run(S.AGENT_FIX)

        self.assertEqual(res.status, "ok")
        self.assertTrue(res.passed)
        self.assertEqual(res.agent.exit_code, 0)
        self.assertFalse(res.agent.timed_out)
        self.assertEqual(res.verifier.exit_code, 0)
        self.assertGreaterEqual(res.agent.duration_s, 0.0)
        self.assertGreaterEqual(res.verifier.duration_s, 0.0)
        self.assertIn("words.py", res.changed_files)
        self.assertFalse(res.accepted)

        # digest is over the patch bytes actually written
        raw = res.patch_path.read_bytes()
        self.assertEqual(res.patch_sha256, hashlib.sha256(raw).hexdigest())
        self.assertIn(b"words.py", raw)

        # the source repo was never written to
        self.assertEqual(S.snapshot(self.repo), before)

        record = hx.inspect_run(res.run_id, runs_root=self.runs)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["patch_sha256"], res.patch_sha256)
        self.assertEqual(record["base_commit"], before["head"])
        self.assertFalse(record["accept"]["accepted"])
        self.assertIn("verifier", record)
        self.assertEqual(record["verifier"]["exit_code"], 0)

    def test_agent_prose_cannot_pass_a_failing_verifier(self) -> None:
        res = self.do_run(S.AGENT_NOOP)
        self.assertEqual(res.status, "verifier_failed")
        self.assertFalse(res.passed)
        self.assertEqual(res.agent.exit_code, 0)
        self.assertEqual(res.verifier.exit_code, 1)
        self.assertIn(
            "all tests certainly pass",
            hx.inspect_run(res.run_id, runs_root=self.runs)["agent"]["stdout_tail"],
        )
        with self.assertRaises(hx.HxError) as ctx:
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertIn("verifier_failed", str(ctx.exception))

    def test_verifier_changes_to_proposed_source_block_accept(self) -> None:
        verifier = [
            S.PY,
            "-c",
            "from pathlib import Path; "
            "p = Path('words.py'); p.write_bytes(p.read_bytes() + b'# changed during verification\\n')",
        ]
        result = self.do_run(S.AGENT_FIX, verify=verifier)
        self.assertEqual(result.status, "verifier_changed_patch")
        self.assertIn("changed during verification", result.patch_path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(hx.AcceptanceRefused, "verifier_changed_patch"):
            hx.accept(result.run_id, runs_root=self.runs)

    def test_verifier_can_create_ignored_build_output(self) -> None:
        verifier = [S.PY, "-c", "from pathlib import Path; Path('output.pyc').touch()"]
        result = self.do_run(S.AGENT_FIX, verify=verifier)
        self.assertTrue(result.passed)
        self.assertNotIn("output.pyc", result.changed_files)
        hx.accept(result.run_id, runs_root=self.runs)

    def test_agent_failure_skips_verifier_and_blocks_accept(self) -> None:
        before = S.snapshot(self.repo)
        res = self.do_run(S.AGENT_BOOM)
        self.assertEqual(res.status, "agent_failed")
        self.assertEqual(res.agent.exit_code, 7)
        self.assertTrue(res.verifier.skipped)
        self.assertIsNone(res.verifier.exit_code)
        self.assertFalse(res.passed)
        self.assertEqual(S.snapshot(self.repo), before)
        with self.assertRaises(hx.HxError):
            hx.accept(res.run_id, runs_root=self.runs)

    def test_agent_timeout_is_measured_and_blocks_accept(self) -> None:
        before = S.snapshot(self.repo)
        res = self.do_run(S.AGENT_HANG, timeout=3)
        self.assertEqual(res.status, "agent_timeout")
        self.assertTrue(res.agent.timed_out)
        self.assertLess(res.agent.duration_s, 60)
        self.assertFalse(res.passed)
        self.assertEqual(S.snapshot(self.repo), before)
        with self.assertRaises(hx.HxError) as ctx:
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_verifier_timeout_blocks_accept(self) -> None:
        res = self.do_run(
            S.AGENT_FIX,
            verify=[S.PY, str(self.sb.script("hang_verify.py", S.AGENT_VERIFIER_HANG))],
            timeout=3,
        )
        self.assertEqual(res.status, "verifier_timeout")
        self.assertTrue(res.verifier.timed_out)
        self.assertFalse(res.passed)
        with self.assertRaises(hx.HxError):
            hx.accept(res.run_id, runs_root=self.runs)

    def test_missing_executable_is_an_error_not_a_pass(self) -> None:
        res = self.do_run(S.AGENT_FIX, verify=["hx-definitely-not-installed-xyz", "--version"])
        self.assertEqual(res.status, "verifier_error")
        self.assertIsNone(res.verifier.exit_code)
        self.assertIsNotNone(res.verifier.error)
        self.assertFalse(res.passed)
        with self.assertRaises(hx.HxError):
            hx.accept(res.run_id, runs_root=self.runs)

    def test_no_changes_is_recorded_and_not_acceptable(self) -> None:
        res = self.do_run(S.AGENT_NOOP, verify=S.VERIFY_TRUE)
        self.assertEqual(res.status, "no_changes")
        self.assertEqual(res.changed_files, [])
        self.assertEqual(res.patch_bytes, 0)
        with self.assertRaises(hx.HxError) as ctx:
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertIn("empty", str(ctx.exception).lower())

    def test_new_untracked_file_is_part_of_the_patch(self) -> None:
        res = self.do_run(S.AGENT_FIX_AND_NEWFILE)
        self.assertEqual(res.status, "ok")
        self.assertIn("RELEASE_NOTES.md", res.changed_files)
        patch = hx.patch_text(res.run_id, runs_root=self.runs)
        self.assertIn("new file mode", patch)
        self.assertIn("RELEASE_NOTES.md", patch)

    # --- argument validation ---------------------------------------------
    def test_empty_verifier_is_rejected(self) -> None:
        with self.assertRaises(hx.HxError):
            hx.run(self.repo, [S.PY, "-c", "pass"], [], runs_root=self.runs)
        with self.assertRaises(hx.HxError):
            hx.run(self.repo, [S.PY, "-c", "pass"], ["   "], runs_root=self.runs)

    def test_empty_agent_argv_is_rejected(self) -> None:
        with self.assertRaises(hx.HxError):
            hx.run(self.repo, [], S.VERIFY_TRUE, runs_root=self.runs)

    def test_non_repository_is_rejected(self) -> None:
        plain = self.sb.path("not-a-repo")
        plain.mkdir()
        with self.assertRaises(hx.HxError):
            hx.run(plain, [S.PY, "-c", "pass"], S.VERIFY_TRUE, runs_root=self.runs)

    def test_bad_timeout_and_pass_env_are_rejected(self) -> None:
        with self.assertRaises(hx.HxError):
            hx.run(
                self.repo,
                [S.PY, "-c", "pass"],
                S.VERIFY_TRUE,
                timeout=0,
                runs_root=self.runs,
            )
        with self.assertRaises(hx.HxError):
            hx.run(
                self.repo,
                [S.PY, "-c", "pass"],
                S.VERIFY_TRUE,
                runs_root=self.runs,
                pass_env=["NOT_A_NAME=1"],
            )

    def test_runs_land_outside_the_source_repo(self) -> None:
        res = self.do_run(S.AGENT_FIX)
        work = res.work_dir.resolve()
        self.assertFalse(str(work).lower().startswith(str(self.repo.resolve()).lower()))
        self.assertTrue(work.is_dir())


if __name__ == "__main__":
    unittest.main()
