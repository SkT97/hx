#!/usr/bin/env python3
"""Deterministic stand-in for a real coding agent, used by ``hx demo`` and tests.

It is not a model and it is not a benchmark: it just edits a file (or refuses
to) so the harness behaviour can be exercised offline, on a synthetic fixture,
with the same result every time. Stdlib only, and it must not import ``hx``:
it runs as a child process with a scrubbed environment.

usage: python _fake_agent.py [fix|talk|newfile|hang]

* ``fix``     - write the corrected file, then claim success in prose
* ``talk``    - change nothing, claim success in prose (verifier must catch it)
* ``newfile`` - ``fix`` plus create a brand new untracked file
* ``hang``    - never finish (timeout path)
"""

from __future__ import annotations

import pathlib
import sys
import time

FIXED_WORDS = '''"""String helper: turn arbitrary text into a URL slug."""

import re


def slugify(text):
    words = re.split(r"[^A-Za-z0-9]+", text)
    return "-".join(w.lower() for w in words if w)
'''

RELEASE_NOTES = "## Unreleased\n\n- slugify now lowercases and joins words with '-'.\n"


def write(path: str, text: str) -> None:
    pathlib.Path(path).write_text(text, encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "fix"
    if mode == "hang":
        print("thinking", flush=True)
        time.sleep(3600)
        return 0
    if mode == "talk":
        print("I have reviewed everything and the change is definitely in place.")
        print("All tests certainly pass - no further action needed.")
        return 0
    if mode not in ("fix", "newfile"):
        print(f"unknown mode {mode!r}", file=sys.stderr)
        return 2
    write("words.py", FIXED_WORDS)
    if mode == "newfile":
        write("RELEASE_NOTES.md", RELEASE_NOTES)
    print(f"fake agent: wrote words.py (mode={mode})")
    print("prose claim: done, verified, ship it  # hx ignores this; the verifier decides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
