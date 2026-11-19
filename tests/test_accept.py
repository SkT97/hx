"""Acceptance of a reviewed patch. Synthetic temporary fixtures only.

Nothing here runs accept against a repository that already existed.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

import hx
from hx import _git
from tests import support as S


class AcceptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()

    def run_ok(self, body: str = S.AGENT_FIX):
        return hx.run(
            self.repo,
            [S.PY, str(self.sb.script("a.py", body))],
            S.VERIFY_UNITTEST,
            timeout=180,
            runs_root=self.runs,
        )

    def test_accept_applies_patch_without_committing(self) -> None:
        res = self.run_ok()
        head_before = _git.head_sha(self.repo)
        out = hx.accept(res.run_id, runs_root=self.runs)
        self.assertTrue(out["accepted"])

        self.assertIn(
            'return "-".join(w.lower() for w in words if w)',
            (self.repo / "words.py").read_text(),
        )
        # explicit accept only: no commit, no push
        self.assertEqual(_git.head_sha(self.repo), head_before)
        _, porcelain, _ = _git.git(["status", "--porcelain"], cwd=self.repo)
        self.assertIn("M words.py", porcelain)
        _, branch, _ = _git.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo)
        self.assertEqual(branch.strip(), "main")

    def test_accept_includes_new_files(self) -> None:
        res = self.run_ok(S.AGENT_FIX_AND_NEWFILE)
        hx.accept(res.run_id, runs_root=self.runs)
        self.assertTrue((self.repo / "RELEASE_NOTES.md").is_file())
        _, porcelain, _ = _git.git(["status", "--porcelain"], cwd=self.repo)
        self.assertIn("?? RELEASE_NOTES.md", porcelain)

    def test_accept_twice_is_refused(self) -> None:
        res = self.run_ok()
        hx.accept(res.run_id, runs_root=self.runs)
        with self.assertRaises(hx.HxError):
            hx.accept(res.run_id, runs_root=self.runs)

    def test_accept_refuses_tampered_patch(self) -> None:
        res = self.run_ok()
        res.patch_path.write_bytes(res.patch_path.read_bytes() + b"\n// tampered\n")
        with self.assertRaises(hx.HxError) as ctx:
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertIn("digest", str(ctx.exception).lower())
        self.assertNotIn("lower()", (self.repo / "words.py").read_text())

    def test_accept_refuses_legacy_record_without_patch_evidence(self) -> None:
        res = self.run_ok()
        record_path = res.run_dir / "run.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.pop("verified_patch_sha256")
        record_path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaisesRegex(hx.AcceptanceRefused, "lacks patch verification evidence"):
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertNotIn("lower()", (self.repo / "words.py").read_text())

    def test_both_git_apply_calls_receive_the_verified_bytes(self) -> None:
        res = self.run_ok()
        verified_bytes = res.patch_path.read_bytes()
        with patch.object(_git.subprocess, "run", wraps=_git.subprocess.run) as run:
            hx.accept(res.run_id, runs_root=self.runs)
        apply_calls = [
            call
            for call in run.call_args_list
            if "apply" in call.args[0] and "--numstat" not in call.args[0]
        ]
        self.assertEqual(len(apply_calls), 2)
        self.assertIn("--check", apply_calls[0].args[0])
        self.assertNotIn("--check", apply_calls[1].args[0])
        for call in apply_calls:
            self.assertEqual(call.args[0][-1], "-")
            self.assertEqual(call.kwargs["input"], verified_bytes)

    def test_accept_refuses_when_head_moved(self) -> None:
        res = self.run_ok()
        (self.repo / "other.txt").write_text("new upstream work\n", encoding="utf-8")
        _git.git(["add", "other.txt"], cwd=self.repo)
        _git.git(["commit", "-m", "upstream moved"], cwd=self.repo)
        with self.assertRaises(hx.HxError) as ctx:
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertIn("HEAD", str(ctx.exception))

    def test_accept_refuses_when_cleanliness_changed(self) -> None:
        res = self.run_ok()
        (self.repo / "surprise.txt").write_text("appeared after the run\n", encoding="utf-8")
        with self.assertRaises(hx.HxError) as ctx:
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertIn("clean", str(ctx.exception).lower())

    def test_dirty_source_is_refused_without_changes(self) -> None:
        repo = self.sb.new_repo(name="dirty-fixture", dirty_note="local-notes.txt")
        before = S.snapshot(repo)
        with self.assertRaisesRegex(hx.HxError, "clean"):
            hx.run(repo, [S.PY, "-c", "pass"], S.VERIFY_TRUE, runs_root=self.runs)
        self.assertEqual(before, S.snapshot(repo))

    def test_accept_refuses_failed_verification(self) -> None:
        res = hx.run(
            self.repo,
            [S.PY, str(self.sb.script("noop.py", S.AGENT_NOOP))],
            S.VERIFY_UNITTEST,
            timeout=180,
            runs_root=self.runs,
        )
        self.assertEqual(res.status, "verifier_failed")
        with self.assertRaises(hx.HxError):
            hx.accept(res.run_id, runs_root=self.runs)

    def test_accept_refuses_timeout(self) -> None:
        res = hx.run(
            self.repo,
            [S.PY, str(self.sb.script("hang.py", S.AGENT_HANG))],
            S.VERIFY_UNITTEST,
            timeout=3,
            runs_root=self.runs,
        )
        with self.assertRaises(hx.HxError):
            hx.accept(res.run_id, runs_root=self.runs)

    def test_unknown_run_id_is_reported(self) -> None:
        with self.assertRaises(hx.HxError):
            hx.inspect_run("20200101T000000Z-deadbeef", runs_root=self.runs)


if __name__ == "__main__":
    unittest.main()


def _new_file_patch(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        "+owned\n"
    )


class AcceptPathGuardTest(unittest.TestCase):
    """A forged record and patch must not widen what accept writes."""

    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()

    def run_ok(self):
        return hx.run(
            self.repo,
            [S.PY, str(self.sb.script("a.py", S.AGENT_FIX))],
            S.VERIFY_UNITTEST,
            timeout=180,
            runs_root=self.runs,
        )

    def forge(self, res, patch_text: str, changed: list[str] | None = None) -> None:
        data = patch_text.encode("utf-8")
        res.patch_path.write_bytes(data)
        record_path = res.run_dir / "run.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(data).hexdigest()
        record["patch_sha256"] = digest
        record["verified_patch_sha256"] = digest
        if changed is not None:
            record["changed_files"] = changed
        record_path.write_text(json.dumps(record), encoding="utf-8")

    def refuses(self, path: str, message: str) -> None:
        res = self.run_ok()
        self.forge(res, _new_file_patch(path))
        with self.assertRaisesRegex(hx.AcceptanceRefused, message):
            hx.accept(res.run_id, runs_root=self.runs)
        _, porcelain, _ = _git.git(["status", "--porcelain"], cwd=self.repo)
        self.assertEqual(porcelain.strip(), "")

    def test_refuses_path_outside_recorded_changes(self) -> None:
        self.refuses("evil.txt", "not in the recorded changed files")

    def test_refuses_dotdot_path(self) -> None:
        self.refuses("../evil.txt", "")

    def test_refuses_git_directory_path(self) -> None:
        self.refuses(".git/hooks/pre-commit", "")

    def test_refuses_absolute_path(self) -> None:
        self.refuses("/tmp/evil.txt", "")

    def test_refuses_old_git(self) -> None:
        res = self.run_ok()
        with patch.object(_git, "git_version", return_value=(2, 31)):
            with self.assertRaisesRegex(hx.AcceptanceRefused, "git 2.32 or newer"):
                hx.accept(res.run_id, runs_root=self.runs)
        self.assertNotIn("lower()", (self.repo / "words.py").read_text())

    def test_refuses_unreadable_git_version(self) -> None:
        res = self.run_ok()
        with patch.object(_git, "git_version", return_value=None):
            with self.assertRaisesRegex(hx.AcceptanceRefused, "unknown"):
                hx.accept(res.run_id, runs_root=self.runs)

    def test_refuses_rename_in_a_forged_patch(self) -> None:
        res = self.run_ok()
        self.forge(
            res,
            "diff --git a/README.md b/words.py\n"
            "similarity index 100%\n"
            "rename from README.md\n"
            "rename to words.py\n",
        )
        with self.assertRaisesRegex(hx.AcceptanceRefused, "rename or copy"):
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertTrue((self.repo / "README.md").is_file())

    def test_the_older_rename_headers_are_refused_too(self) -> None:
        res = self.run_ok()
        self.forge(
            res,
            "diff --git a/README.md b/ok2.txt\n"
            "similarity index 100%\n"
            "rename old README.md\n"
            "rename new ok2.txt\n",
        )
        with self.assertRaisesRegex(hx.AcceptanceRefused, "rename or copy"):
            hx.accept(res.run_id, runs_root=self.runs)
        self.assertTrue((self.repo / "README.md").is_file())
        self.assertFalse((self.repo / "ok2.txt").exists())
