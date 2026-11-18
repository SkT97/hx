"""Isolation guarantees of a run: env scrubbing, private HOME, no push.

These are file-system conveniences, not an OS security boundary; the tests
check exactly what the README claims and nothing more.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import hx
from hx import _env, _git
from tests import support as S

SECRET_NAME = "HX_TEST_SECRET"
SECRET_VALUE = "s3cr3t-do-not-serialize"
SCRUBBED_NAME = "HX_TEST_SCRUBBED"
SCRUBBED_VALUE = "ambient-leak-do-not-pass"


class EnvScrubbingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = S.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.repo = self.sb.new_repo()
        self.runs = self.sb.runs_root()
        os.environ[SECRET_NAME] = SECRET_VALUE
        os.environ[SCRUBBED_NAME] = SCRUBBED_VALUE
        self.addCleanup(os.environ.pop, SECRET_NAME, None)
        self.addCleanup(os.environ.pop, SCRUBBED_NAME, None)

    def run_probe(self):
        return hx.run(
            self.repo,
            [S.PY, str(self.sb.script("dump.py", S.AGENT_DUMP_ENV)), self.runs],
            S.VERIFY_TRUE,
            timeout=180,
            runs_root=self.runs,
            pass_env=[SECRET_NAME],
        )

    def dump(self, res) -> dict:
        return json.loads((res.work_dir / "envdump.json").read_text(encoding="utf-8"))

    def test_only_allowlisted_and_requested_names_reach_the_agent(self) -> None:
        res = self.run_probe()
        env = self.dump(res)
        self.assertEqual(env.get(SECRET_NAME), SECRET_VALUE)
        self.assertNotIn(SCRUBBED_NAME, env)
        self.assertTrue(env.get("PATH"), "PATH must survive scrubbing")

    def test_agent_gets_a_private_home_and_tmp(self) -> None:
        res = self.run_probe()
        env = self.dump(res)
        home = env.get("HOME") or env.get("USERPROFILE")
        self.assertTrue(home, "isolated HOME must be set")
        self.assertTrue(Path(home).resolve().is_relative_to(res.run_dir.resolve()))
        self.assertNotEqual(Path(home).resolve(), Path.home().resolve())
        tmp = env.get("TMPDIR") or env.get("TMP")
        self.assertTrue(Path(tmp).resolve().is_relative_to(res.run_dir.resolve()))

    def test_secret_values_are_never_serialized(self) -> None:
        res = self.run_probe()
        record_text = (res.run_dir / "run.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET_VALUE, record_text)
        self.assertIn(SECRET_NAME, record_text)
        record = hx.inspect_run(res.run_id, runs_root=self.runs)
        self.assertIn(SECRET_NAME, record["pass_env_names"])
        self.assertIn(SECRET_NAME, record["env_names"])
        self.assertNotIn(SCRUBBED_NAME, record["env_names"])
        # the agent printed the secret; hx masks it in captured output
        self.assertNotIn(SECRET_VALUE, record["agent"]["stdout_tail"])
        self.assertIn("hx:masked", record["agent"]["stdout_tail"])

    def test_ambient_git_config_injection_is_dropped(self) -> None:
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "core.hooksPath"
        os.environ["GIT_CONFIG_VALUE_0"] = "/tmp/evil"
        self.addCleanup(os.environ.pop, "GIT_CONFIG_COUNT", None)
        self.addCleanup(os.environ.pop, "GIT_CONFIG_KEY_0", None)
        self.addCleanup(os.environ.pop, "GIT_CONFIG_VALUE_0", None)
        res = self.run_probe()
        env = self.dump(res)
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertNotIn("GIT_CONFIG_KEY_0", env)

    def test_push_is_disabled_on_the_clone(self) -> None:
        res = hx.run(
            self.repo,
            [S.PY, str(self.sb.script("push.py", S.AGENT_PUSH_PROBE)), self.runs],
            S.VERIFY_TRUE,
            timeout=180,
            runs_root=self.runs,
        )
        probe = (res.work_dir / "push_probe.txt").read_text(encoding="utf-8")
        rc = int(probe.split("=", 1)[1].splitlines()[0])
        self.assertNotEqual(rc, 0, f"push should have been blocked, probe said: {probe!r}")
        _, pushurl, _ = _git.git(["config", "--get", "remote.origin.pushurl"], cwd=res.work_dir)
        self.assertTrue(pushurl.strip())

    def test_argv_secret_looking_values_are_redacted_in_records(self) -> None:
        argv = [S.PY, "-c", "pass", "--api-key=super-private", "--verbose"]
        red = _env.redact_argv(argv)
        self.assertNotIn("super-private", " ".join(red))
        self.assertIn("--verbose", red)

    def test_no_pycache_noise_in_the_patch(self) -> None:
        res = hx.run(
            self.repo,
            [S.PY, str(self.sb.script("dump2.py", S.AGENT_DUMP_ENV)), self.runs],
            S.VERIFY_TRUE,
            timeout=180,
            runs_root=self.runs,
        )
        self.assertFalse([f for f in res.changed_files if f.endswith(".pyc") or "__pycache__" in f])


if __name__ == "__main__":
    unittest.main()
