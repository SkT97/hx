"""Shell-free wrapper around the git CLI.

Every git call in hx goes through :func:`git`: argv only (never a shell), a
captured environment and an explicit timeout. Ambient ``GIT_CONFIG*`` command
line config is dropped so a stray injected setting cannot silently change what
hx's own git commands do.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

GIT = "git"

#: Push target set on every clone: no such protocol exists, so a push always fails.
PUSH_DISABLED_URL = "hx+no-push://disabled.invalid"


class GitError(RuntimeError):
    """A git command failed or could not be started."""


def git_env() -> dict[str, str]:
    """Environment for hx's own git calls (ambient GIT_CONFIG* injection removed)."""
    env = {k: v for k, v in os.environ.items() if not str(k).upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_ADVICE"] = "0"
    return env


def git(
    args: list[str],
    cwd: Path | str | None = None,
    check: bool = True,
    timeout: float = 600.0,
    binary: bool = False,
    stdin: bytes | None = None,
) -> tuple[int, str | bytes, str | bytes]:
    """Run ``git <args>`` without a shell and return ``(rc, stdout, stderr)``."""
    argv = [GIT, *[str(a) for a in args]]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=git_env(),
            capture_output=True,
            input=stdin,
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # pragma: no cover - depends on the machine
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(argv[1:])} timed out after {timeout}s") from exc
    except OSError as exc:  # pragma: no cover - depends on the machine
        raise GitError(f"could not run git: {exc}") from exc

    out: str | bytes = proc.stdout if binary else _decode(proc.stdout)
    err: str | bytes = proc.stderr if binary else _decode(proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(argv[1:])} failed (rc={proc.returncode}): {_decode(proc.stderr).strip()[:400]}"
        )
    return proc.returncode, out, err


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def is_work_tree(path: Path) -> bool:
    rc, out, _ = git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
    return rc == 0 and out.strip() == "true"


def work_tree_root(path: Path) -> Path | None:
    """Resolved top level of the work tree containing ``path``, or ``None``.

    Git is asked (``rev-parse --show-toplevel``) instead of probing for a ``.git``
    entry, so a subdirectory of a checkout never looks like a repository root,
    while a linked work tree still reports its own root.
    """
    rc, out, _ = git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    top = str(out).strip()
    if rc != 0 or not top:
        return None
    return Path(top).resolve()


def head_sha(repo: Path) -> str:
    _, out, _ = git(["rev-parse", "HEAD"], cwd=repo)
    return out.strip()


def dirty_paths(repo: Path) -> list[str]:
    """Sorted paths with local modifications or untracked, as git reports them."""
    _, out, _ = git(["status", "--porcelain"], cwd=repo)
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        body = line[3:]
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        paths.append(body.strip().strip('"'))
    return sorted(paths)


def clone(source: Path, dest: Path, base: str, timeout: float = 600.0) -> None:
    """Fresh local clone of ``source`` at ``base``, with push disabled.

    ``dest`` must not exist yet. Object files are copied (``--no-hardlinks``) so
    the clone does not share the source object store.
    """
    top = work_tree_root(source)
    if top is not None and top != Path(source).resolve():
        raise GitError(
            f"clone source {source} is a subdirectory of the work tree {top}; "
            "clone the work tree root instead"
        )
    git(
        [
            "-c",
            "core.autocrlf=false",
            "clone",
            "--no-hardlinks",
            "--no-checkout",
            str(source),
            str(dest),
        ],
        timeout=timeout,
    )
    rc, _, _ = git(["cat-file", "-e", f"{base}^{{commit}}"], cwd=dest, check=False)
    if rc != 0:
        raise GitError(
            f"the clone does not contain commit {base}; run hx with HEAD on a branch "
            "so the commit is reachable from a ref"
        )
    for key, value in (
        ("core.autocrlf", "false"),
        ("user.name", "hx run"),
        ("user.email", "hx@localhost.invalid"),
        ("commit.gpgsign", "false"),
        ("remote.origin.pushurl", PUSH_DISABLED_URL),
    ):
        git(["config", key, value], cwd=dest)
    git(
        ["-c", "advice.detachedHead=false", "checkout", "--detach", "--force", base],
        cwd=dest,
    )


def stage_all(work: Path, timeout: float = 600.0) -> None:
    """Stage everything (including new files) so the diff can include them.

    This only touches the throwaway clone, never the source repository.
    """
    git(["add", "-A"], cwd=work, timeout=timeout)


def staged_patch(work: Path, base: str, timeout: float = 600.0) -> bytes:
    _, out, _ = git(
        ["diff", "--cached", "--binary", "--no-renames", base],
        cwd=work,
        timeout=timeout,
        binary=True,
    )
    return out  # type: ignore[return-value]


def staged_names(work: Path, base: str, timeout: float = 600.0) -> list[str]:
    _, out, _ = git(
        ["-c", "core.quotepath=false", "diff", "--cached", "--name-only", "--no-renames", base],
        cwd=work,
        timeout=timeout,
    )
    return [line for line in str(out).splitlines() if line.strip()]


#: Older ``git apply`` does not refuse paths that leave the work tree or enter ``.git``.
MIN_GIT_VERSION = (2, 32)


def git_version() -> tuple[int, int] | None:
    """``(major, minor)`` of the installed git, or ``None`` if it cannot be read."""
    rc, out, _ = git(["--version"], check=False)
    match = re.match(r"git version (\d+)\.(\d+)", str(out))
    return (int(match[1]), int(match[2])) if rc == 0 and match else None


def has_rename_or_copy(patch: bytes) -> bool:
    """Whether any header line of the patch starts a rename or a copy.

    ``git apply`` reads several spellings (``rename from``, ``rename old``, ``copy
    to`` and others) and ``--numstat`` reports only the new name of a rename, so
    the old file would go unchecked. Body lines of a diff start with a space,
    ``+``, ``-`` or a backslash, so a line starting with these words is a header.
    """
    return any(line.startswith((b"rename ", b"copy ")) for line in patch.splitlines())


def patch_paths(repo: Path, patch: bytes, timeout: float = 600.0) -> list[str]:
    """Paths a patch touches, as git itself reads them (``git apply --numstat -z``).

    A pure rename or copy reports only its new name, so callers must refuse those
    separately. Raises :class:`GitError` when git cannot parse the patch.
    """
    rc, out, err = git(
        ["apply", "--numstat", "-z", "-p1", "-"],
        cwd=repo,
        check=False,
        timeout=timeout,
        stdin=patch,
    )
    if rc != 0:
        raise GitError(f"git apply --numstat could not read the patch: {str(err).strip()[:400]}")
    paths = []
    for entry in str(out).split("\0"):
        if not entry:
            continue
        fields = entry.split("\t", 2)
        if len(fields) != 3 or not fields[2]:
            raise GitError("unexpected git apply --numstat output")
        paths.append(fields[2])
    return paths


def apply_patch(
    repo: Path, patch: bytes, check_only: bool = False, timeout: float = 600.0
) -> tuple[int, str, str]:
    """``git apply`` (or ``--check``) the supplied bytes. Never stages, commits or pushes."""
    args = [
        "-c",
        "core.autocrlf=false",
        "apply",
        "-p1",
        "--binary",
        "--whitespace=nowarn",
    ]
    if check_only:
        args.append("--check")
    args.append("-")
    rc, out, err = git(args, cwd=repo, check=False, timeout=timeout, stdin=patch)
    return rc, str(out), str(err)
