"""Environment construction for agent and verifier processes.

Default deny: only the names in :data:`BASE_ALLOWLIST` (plus the Windows
runtime essentials) reach a child process, and whatever the caller explicitly
asked for through ``pass_env``. Values of the passed names are never written
into hx records: they are masked in captured output and only names are stored.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Anything not listed here is not passed on to the agent or the verifier.
BASE_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "TMPDIR",
        "TZ",
        "NO_COLOR",
        "FORCE_COLOR",
    }
)

#: Needed by the Windows loader / C runtime and by common tooling.
WINDOWS_ALLOWLIST = frozenset(
    {
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERDOMAIN",
        "USERDOMAIN_ROAMINGPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "ALLUSERSPROFILE",
        "COMMONPROGRAMFILES",
    }
)

#: Replacement written instead of a secret value.
MASK = "hx:masked"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_HINT = re.compile(
    r"(key|token|secret|password|passwd|credential|authorization|auth|session)",
    re.IGNORECASE,
)


def validate_pass_env(pass_env: object) -> tuple[str, ...]:
    """Validate explicitly requested variable names (names only, never values)."""
    if pass_env is None:
        return ()
    if isinstance(pass_env, str):
        raise ValueError("pass_env must be a sequence of variable names, not one string")
    names: list[str] = []
    for item in pass_env:
        name = str(item).strip()
        if not _NAME_RE.match(name):
            raise ValueError(
                f"pass_env entry {item!r} is not a variable name; pass names only, e.g. 'MY_TOKEN'"
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def is_allowed(name: str) -> bool:
    upper = name.upper()
    return upper in BASE_ALLOWLIST or upper in WINDOWS_ALLOWLIST


def isolated_env(
    pass_env: object = (), home: Path | None = None, tmp: Path | None = None
) -> tuple[dict[str, str], list[str]]:
    """Build a scrubbed child environment plus the sorted list of names it holds.

    ``home`` becomes HOME/USERPROFILE and ``tmp`` becomes TMPDIR/TMP/TEMP, so a
    agent that writes to ``~`` or to a temp dir stays inside the run directory.
    """
    requested = set(validate_pass_env(pass_env))
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        name = str(key)
        if name.upper().startswith("GIT_CONFIG"):
            continue  # command-line config injection must not follow the agent around
        if is_allowed(name) or name in requested:
            env[name] = value

    if home is not None:
        home_str = str(home)
        env["HOME"] = home_str
        env["USERPROFILE"] = home_str
        drive, tail = os.path.splitdrive(home_str)
        if drive:
            env["HOMEDRIVE"] = drive
            env["HOMEPATH"] = tail
    if tmp is not None:
        tmp_str = str(tmp)
        env["TMPDIR"] = tmp_str
        env["TMP"] = tmp_str
        env["TEMP"] = tmp_str

    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keeps __pycache__ out of the measured diff
    return env, sorted(env)


def secret_values(names: object) -> list[str]:
    """Current values of the explicitly passed names, for masking records."""
    values: list[str] = []
    for name in names or ():
        value = os.environ.get(str(name))
        if value:
            values.append(value)
    return values


def mask_values(text: str, values: list[str]) -> str:
    """Replace known secret values in captured output with :data:`MASK`.

    Every explicitly forwarded value is masked, including short values.
    """
    if not text:
        return text
    for value in sorted(set(values), key=len, reverse=True):
        if not value:
            continue
        text = text.replace(value, MASK)
    return text


def redact_argv(argv: list[str]) -> list[str]:
    """Copy of an argv with secret-looking option values replaced by ``***``."""
    out: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        name, sep, _value = token.partition("=")
        if sep and _SECRET_HINT.search(name):
            out.append(f"{name}=***")
            continue
        if token.startswith("-") and _SECRET_HINT.search(token):
            redact_next = True
            out.append(token)
            continue
        out.append(token)
    return out
