"""Shared helpers for the hx test-suite.

Every repository used here is a synthetic, throwaway Git repository created
under a temporary directory owned by the test. Nothing in this suite reads,
writes or accepts into a repository that already existed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from hx import _git

PY = sys.executable
GIT = "git"

WORDS_BUGGY = '''"""Tiny string helper that is deliberately broken."""

import re


def slugify(text):
    words = re.split(r"[^A-Za-z0-9]+", text)
    return "_".join(w for w in words if w)
'''

WORDS_FIXED = '''"""Tiny string helper that is deliberately broken."""

import re


def slugify(text):
    words = re.split(r"[^A-Za-z0-9]+", text)
    return "-".join(w.lower() for w in words if w)
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

GITIGNORE = "__pycache__/\n*.pyc\n"

BUG_LINE = 'return "_".join(w for w in words if w)'
FIX_LINE = 'return "-".join(w.lower() for w in words if w)'

# --- agent script bodies (self-contained, stdlib only) ---------------------

AGENT_FIX = f"""import pathlib

p = pathlib.Path("words.py")
src = p.read_text(encoding="utf-8")
out = src.replace({BUG_LINE!r}, {FIX_LINE!r})
assert out != src, "bug line not found"
p.write_text(out, encoding="utf-8")
print("DONE - everything is perfect, please ship it (prose only)")
"""

AGENT_FIX_AND_NEWFILE = (
    AGENT_FIX
    + """
pathlib.Path("RELEASE_NOTES.md").write_text("notes written by the agent\\n", encoding="utf-8")
"""
)

AGENT_NOOP = 'print("I fixed it, all tests certainly pass")  # touches nothing\n'

AGENT_BOOM = 'import sys\n\nprint("starting")\nsys.exit(7)\n'

AGENT_HANG = 'import time\n\nprint("thinking")\ntime.sleep(300)\n'

AGENT_VERIFIER_HANG = "import sys, time\n\nprint('verifier thinking')\ntime.sleep(300)\n"

AGENT_DUMP_ENV = """import json
import os
import pathlib

pathlib.Path("envdump.json").write_text(
    json.dumps(dict(os.environ), indent=1, sort_keys=True), encoding="utf-8"
)
print("SECRET=" + os.environ.get("HX_TEST_SECRET", "<unset>"))
print("SCRUBBED=" + os.environ.get("HX_TEST_SCRUBBED", "<unset>"))
print("HOME=" + (os.environ.get("HOME") or os.environ.get("USERPROFILE") or "<unset>"))
print("TMP=" + (os.environ.get("TMPDIR") or os.environ.get("TMP") or "<unset>"))
"""

AGENT_PUSH_PROBE = """import pathlib
import subprocess

proc = subprocess.run(
    ["git", "push", "origin", "HEAD"],
    capture_output=True,
    text=True,
    errors="replace",
)
pathlib.Path("push_probe.txt").write_text(
    "rc=%s\\n%s\\n%s\\n" % (proc.returncode, proc.stdout, proc.stderr), encoding="utf-8"
)
print("push rc=%s" % proc.returncode)
"""

VERIFY_UNITTEST = [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
VERIFY_TRUE = [PY, "-c", "print('verifier ok')"]


def force_rmtree(path: Path) -> None:
    """Remove a tree even when it contains read-only git object files."""
    path = Path(path)
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        for name in list(dirs) + list(files):
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


class Sandbox:
    """Scratch area for one test: fixture repos, run roots, agent scripts."""

    def __init__(self, prefix: str = "hx-test-") -> None:
        self.root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()

    def cleanup(self) -> None:
        force_rmtree(self.root)

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def runs_root(self) -> Path:
        runs = self.root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        return runs

    def script(self, name: str, body: str) -> Path:
        target = self.root / "agents" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
        return target

    def new_repo(self, name: str = "fixture", dirty_note: str | None = None) -> Path:
        """Create a committed synthetic repo whose verifier fails until fixed."""
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "words.py").write_text(WORDS_BUGGY, encoding="utf-8", newline="\n")
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / "__init__.py").write_text("")
        (repo / "tests" / "test_words.py").write_text(TEST_WORDS, encoding="utf-8", newline="\n")
        (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8", newline="\n")
        (repo / "README.md").write_text("Synthetic fixture repo.\n", encoding="utf-8", newline="\n")
        _git.git(["init", "-b", "main"], cwd=repo)
        _git.git(["config", "user.email", "hx-test@example.invalid"], cwd=repo)
        _git.git(["config", "user.name", "hx test"], cwd=repo)
        _git.git(["config", "commit.gpgsign", "false"], cwd=repo)
        _git.git(["add", "-A"], cwd=repo)
        _git.git(["commit", "-m", "initial fixture"], cwd=repo)
        if dirty_note:
            (repo / dirty_note).write_text("pre-existing local note\n", encoding="utf-8")
        return repo


def file_hashes(repo: Path) -> dict[str, str]:
    """sha256 of every tracked-or-not file in the tree (excluding .git)."""
    out: dict[str, str] = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            full = Path(root) / name
            rel = full.relative_to(repo).as_posix()
            h = hashlib.sha256()
            h.update(full.read_bytes())
            out[rel] = h.hexdigest()
    return out


def snapshot(repo: Path) -> dict:
    """Full fingerprint of a source repo: HEAD, porcelain status, file hashes."""
    _, head, _ = _git.git(["rev-parse", "HEAD"], cwd=repo)
    _, porcelain, _ = _git.git(["status", "--porcelain"], cwd=repo)
    return {
        "head": head.strip(),
        "porcelain": porcelain,
        "files": file_hashes(repo),
    }


def verifier_output_is_failing(repo: Path) -> bool:
    """Sanity check on the fixture itself: the unittest verifier must fail here."""
    proc = subprocess.run(
        VERIFY_UNITTEST, cwd=str(repo), capture_output=True, text=True, errors="replace"
    )
    return proc.returncode != 0
