"""``hx demo``: the whole loop, offline, on a synthetic throwaway repository.

No network, no real model, no benchmark and no user repository: hx creates a
small git repo under a temp directory, lets the deterministic fake agent
:mod:`hx._fake_agent` work on it, shows that a talking-only agent is blocked and
that a verified patch is applied by an explicit accept only.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from . import _git
from ._core import HxError, accept, inspect_run, patch_text, run

WORDS_BUGGY = '''"""String helper: turn arbitrary text into a URL slug."""

import re


def slugify(text):
    words = re.split(r"[^A-Za-z0-9]+", text)
    return "_".join(w for w in words if w)
'''

TEST_WORDS = """import unittest

from words import slugify


class SlugifyTest(unittest.TestCase):
    def test_lowercase_dash(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation(self):
        self.assertEqual(slugify("Debian! GNU/Linux 12"), "debian-gnu-linux-12")


if __name__ == "__main__":
    unittest.main()
"""


def _force_rmtree(path: Path) -> None:
    """Git marks objects read-only on Windows; clear the bit before deleting."""
    for root, dirs, files in os.walk(str(path)):
        for name in list(dirs) + list(files):
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _make_fixture(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "words.py").write_text(WORDS_BUGGY, encoding="utf-8", newline="\n")
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "tests" / "__init__.py").write_text("")
    (dest / "tests" / "test_words.py").write_text(TEST_WORDS, encoding="utf-8", newline="\n")
    (dest / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8", newline="\n")
    (dest / "README.md").write_text(
        "Synthetic fixture created by `hx demo`. Safe to delete.\n",
        encoding="utf-8",
        newline="\n",
    )
    _git.git(["init", "-b", "main"], cwd=dest)
    _git.git(["config", "user.email", "hx-demo@localhost.invalid"], cwd=dest)
    _git.git(["config", "user.name", "hx demo"], cwd=dest)
    _git.git(["config", "commit.gpgsign", "false"], cwd=dest)
    _git.git(["add", "-A"], cwd=dest)
    _git.git(["commit", "-m", "fixture: slugify is broken, tests fail"], cwd=dest)
    return dest


def _step_line(label: str, step) -> str:
    if step.skipped:
        state = "skipped"
    elif step.timed_out:
        state = f"TIMEOUT after {step.duration_s:.2f}s"
    elif step.exit_code is None:
        state = f"not started ({step.error})"
    else:
        state = f"rc={step.exit_code} in {step.duration_s:.2f}s"
    return f"    {label:<9} {state}"


def demo(runs_root: Path | str | None = None, keep: bool = False, timeout: float = 300.0) -> int:
    """Run the full loop; returns 0 when the whole story behaved as documented."""
    scratch = Path(tempfile.mkdtemp(prefix="hx-demo-")).resolve()
    runs = Path(runs_root).expanduser() if runs_root else scratch / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    fixture = _make_fixture(scratch / "fixture")
    agent = Path(__file__).with_name("_fake_agent.py")
    py = sys.executable
    verify = [py, "-m", "unittest", "discover", "-s", "tests", "-t", "."]

    print("hx demo - synthetic temporary fixture, deterministic fake agent, no network.")
    print(f"    fixture: {fixture}  HEAD {_git.head_sha(fixture)[:12]}")
    print("    verifier: `python -m unittest discover` (fails until someone fixes the code)")
    ok = True
    try:
        print("\n[1/3] an agent that only talks: verification must block it")
        talk = run(fixture, [py, str(agent), "talk"], verify, timeout=timeout, runs_root=runs)
        print(f"    status={talk.status}")
        print(_step_line("agent:", talk.agent))
        print(_step_line("verifier:", talk.verifier))
        try:
            accept(talk.run_id, runs_root=runs)
            print("    UNEXPECTED: accept applied an unverified patch")
            ok = False
        except HxError as exc:
            print(f"    accept refused: {exc}")

        print("\n[2/3] the fake agent writes the fix; the verifier decides the verdict")
        good = run(fixture, [py, str(agent), "fix"], verify, timeout=timeout, runs_root=runs)
        rec = inspect_run(good.run_id, runs_root=runs)
        print(
            f"    status={good.status} files={len(good.changed_files)} "
            f"bytes={good.patch_bytes} sha256={good.patch_sha256}"
        )
        print(_step_line("agent:", good.agent))
        print(_step_line("verifier:", good.verifier))
        print(f"    env names handed over ({len(rec['env_names'])}): {', '.join(rec['env_names'])}")
        print("\n    reviewable patch, pending human acceptance:")
        for line in patch_text(good.run_id, runs_root=runs).splitlines():
            print(f"    | {line}")

        print("\n[3/3] explicit accept applies it to the synthetic fixture (nothing is committed)")
        out = accept(good.run_id, runs_root=runs)
        print(
            f"    applied={out['applied_files']} committed={out['committed']} "
            f"pushed={out['pushed']} fixture HEAD still {_git.head_sha(fixture)[:12]}"
        )
        _, porcelain, _ = _git.git(["status", "--porcelain"], cwd=fixture)
        print(f"    fixture working tree: {porcelain.strip() or '(clean)'}")
        ok = ok and good.passed and out["accepted"] and not out["committed"]
        print(f"\nrun records kept under {runs} (hx inspect <run_id> / hx diff <run_id>)")
        return 0 if ok else 1
    finally:
        if keep:
            print(f"demo scratch kept at {scratch}")
        else:
            _force_rmtree(scratch)
