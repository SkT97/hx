"""Offline check of a recorded run and its in-toto statement. Synthetic fixtures only."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unittest

import hx
from hx.cli import main
from tests import support as S


class AttestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()
        self.res = hx.run(
            self.repo,
            [S.PY, str(self.sb.script("a.py", S.AGENT_FIX))],
            S.VERIFY_UNITTEST,
            timeout=180,
            runs_root=self.runs,
        )

    def edit_record(self, **changes) -> None:
        path = self.res.run_dir / "run.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            record[key] = value
        path.write_text(json.dumps(record), encoding="utf-8")

    def failed(self) -> set[str]:
        report = hx.check_run(self.res.run_id, runs_root=self.runs)
        return {c["name"] for c in report["checks"] if not c["ok"]}

    def test_a_fresh_run_passes_every_check(self) -> None:
        report = hx.check_run(self.res.run_id, runs_root=self.runs)
        self.assertTrue(report["ok"], report)
        self.assertEqual(
            {c["name"] for c in report["checks"]},
            {
                "patch_file_name",
                "patch_digest",
                "verified_digest",
                "passed_flag",
                "status_from_exits",
                "changed_files_match_patch",
            },
        )

    def test_a_modified_patch_is_caught(self) -> None:
        self.res.patch_path.write_bytes(self.res.patch_path.read_bytes() + b"\n")
        self.assertIn("patch_digest", self.failed())

    def test_a_same_size_patch_change_is_caught_by_the_digest(self) -> None:
        raw = self.res.patch_path.read_bytes()
        self.res.patch_path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        report = hx.check_run(self.res.run_id, runs_root=self.runs)
        detail = next(c["detail"] for c in report["checks"] if c["name"] == "patch_digest")
        self.assertIn("patch_digest", self.failed())
        self.assertTrue(detail.startswith("sha256"))

    def test_a_missing_verification_digest_is_caught(self) -> None:
        self.edit_record(verified_patch_sha256=None)
        self.assertIn("verified_digest", self.failed())

    def test_a_forged_status_is_caught_by_the_recorded_exits(self) -> None:
        record = json.loads((self.res.run_dir / "run.json").read_text(encoding="utf-8"))
        record["verifier"]["exit_code"] = 1
        self.edit_record(verifier=record["verifier"])
        self.assertIn("status_from_exits", self.failed())

    def test_a_rename_in_the_patch_is_never_ok(self) -> None:
        forged = (
            b"diff --git a/README.md b/ok2.txt\n"
            b"similarity index 100%\n"
            b"rename old README.md\n"
            b"rename new ok2.txt\n"
        )
        self.res.patch_path.write_bytes(forged)
        self.edit_record(
            patch_sha256=hashlib.sha256(forged).hexdigest(),
            verified_patch_sha256=hashlib.sha256(forged).hexdigest(),
            patch_bytes=len(forged),
            changed_files=["ok2.txt"],
            change_count=1,
        )
        report = hx.check_run(self.res.run_id, runs_root=self.runs)
        self.assertIn(
            "changed_files_match_patch", {c["name"] for c in report["checks"] if not c["ok"]}
        )
        with self.assertRaises(hx.HxError):
            hx.attest_run(self.res.run_id, runs_root=self.runs)

    def test_a_forged_changed_file_list_is_caught(self) -> None:
        self.edit_record(changed_files=["something_else.py"], change_count=1)
        self.assertIn("changed_files_match_patch", self.failed())

    def test_a_forged_passed_flag_is_caught(self) -> None:
        self.edit_record(passed=False)
        self.assertIn("passed_flag", self.failed())

    def test_the_statement_follows_in_toto_v1_and_leaks_no_local_path(self) -> None:
        statement = hx.attest_run(self.res.run_id, runs_root=self.runs)
        self.assertEqual(statement["_type"], "https://in-toto.io/Statement/v1")
        digest = hashlib.sha256(self.res.patch_path.read_bytes()).hexdigest()
        self.assertEqual(
            statement["subject"], [{"name": "patch.diff", "digest": {"sha256": digest}}]
        )
        self.assertTrue(statement["predicateType"].startswith("https://"))
        predicate = statement["predicate"]
        self.assertEqual(predicate["status"], "ok")
        self.assertTrue(predicate["passed"])
        self.assertEqual(predicate["base_commit"], self.res.base_commit)
        self.assertIn("words.py", predicate["changed_files"])
        text = json.dumps(statement)
        for private in (str(self.repo), str(self.res.run_dir), "stdout_tail"):
            self.assertNotIn(private.replace("\\", "\\\\"), text)

    def test_no_statement_for_a_run_that_fails_its_check(self) -> None:
        self.res.patch_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(hx.HxError, "patch_digest"):
            hx.attest_run(self.res.run_id, runs_root=self.runs)

    def test_cli_check_and_attest(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["check", self.res.run_id, "--runs-root", str(self.runs)])
        self.assertEqual(code, 0)
        self.assertIn("patch_digest", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["attest", self.res.run_id, "--runs-root", str(self.runs)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["_type"], "https://in-toto.io/Statement/v1")
        self.res.patch_path.write_bytes(b"tampered")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["check", self.res.run_id, "--runs-root", str(self.runs)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
