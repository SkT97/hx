"""hx core: delegate work to a coding agent, verify it, measure it, and keep the
result as a reviewable patch that only an explicit accept applies.

Isolation model (read this before trusting it):

* the agent works in a **fresh local clone** of your repository, outside the
  source directory, pinned to the commit HEAD had when the run started;
* pushing from that clone is disabled;
* the agent gets a scrubbed, allowlisted environment and a private HOME/tmp;
* **this is not an OS security boundary.** A determined process running as you
  can still touch anything you can. Treat agent output as untrusted input, and
  read the patch: hx itself writes back only an explicitly accepted patch; it
  does not sandbox the agent's syscalls.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import _env, _git
from ._git import GitError
from ._process import execute

VERSION = "0.1.0"
SCHEMA_VERSION = 1
TAIL_CHARS = 4000
PATCH_NAME = "patch.diff"
RECORD_NAME = "run.json"

STATUS_OK = "ok"


class HxError(RuntimeError):
    """Something hx could not do (bad arguments, no git, unresolvable run)."""


class AcceptanceRefused(HxError):
    """An explicit accept was refused; nothing was written to the target."""


@dataclass
class StepResult:
    """One measured process: agent or verifier."""

    argv: list[str]
    exit_code: int | None = None
    timed_out: bool = False
    skipped: bool = False
    duration_s: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": _env.redact_argv(self.argv),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "skipped": self.skipped,
            "duration_s": round(self.duration_s, 3),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "error": self.error,
        }


@dataclass
class RunResult:
    """Outcome of one hx run, already recorded on disk."""

    run_id: str
    run_dir: Path
    work_dir: Path
    home_dir: Path
    source_repo: Path
    base_commit: str
    source_clean: bool
    source_dirty_files: list[str]
    agent: StepResult
    verifier: StepResult
    status: str
    passed: bool
    changed_files: list[str]
    patch_path: Path
    patch_sha256: str
    patch_bytes: int
    env_names: list[str] = field(default_factory=list)
    pass_env_names: list[str] = field(default_factory=list)
    created_at: str = ""
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return bool(self.record.get("accept", {}).get("accepted"))


def default_runs_root() -> Path:
    """Where run directories live: ``$HX_RUNS_ROOT`` or ``~/.hx/runs`` (outside sources)."""
    env = os.environ.get("HX_RUNS_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hx" / "runs"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)


def _normalize_argv(argv: Sequence[str], kind: str) -> list[str]:
    if isinstance(argv, str):
        raise HxError(f"{kind}_argv must be a list of program arguments, not a string")
    items = [str(item) for item in (argv or [])]
    if not items or not items[0].strip():
        raise HxError(f"{kind}_argv is empty: hx needs an explicit {kind} command argv to run")
    return items


def _to_str(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw or "")


def _tail(text: str, mask: list[str]) -> str:
    return _env.mask_values(text, mask)[-TAIL_CHARS:]


def _run_step(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float, mask: list[str]
) -> StepResult:
    """Run one argv without a shell and measure exit code, wall time and output."""
    step = StepResult(argv=[_env.mask_values(arg, mask) for arg in argv])
    start = time.monotonic()
    longest_secret = max(map(len, mask), default=0)
    output_limit = 4 * (TAIL_CHARS + longest_secret + 1)

    def captured_tail(raw: bytes | None) -> str:
        masked = _env.mask_values(_to_str(raw), mask)
        if raw and len(raw) == output_limit:
            # A retained suffix might start inside a secret or UTF-8 sequence.
            # Drop that prefix after masking all complete occurrences.
            masked = masked[longest_secret + 1 :]
        return masked[-TAIL_CHARS:]

    try:
        proc = execute(argv, cwd=cwd, env=env, timeout=timeout, output_limit=output_limit)
    except subprocess.TimeoutExpired as exc:
        step.timed_out = True
        step.duration_s = time.monotonic() - start
        step.error = f"timed out after {timeout:g}s"
        step.stdout_tail = captured_tail(exc.stdout)
        step.stderr_tail = captured_tail(exc.stderr)
        return step
    except FileNotFoundError as exc:
        step.duration_s = time.monotonic() - start
        step.error = _env.mask_values(f"executable not found: {argv[0]!r}", mask)
        step.stderr_tail = _tail(str(exc), mask)
        return step
    except OSError as exc:
        step.duration_s = time.monotonic() - start
        step.error = _env.mask_values(f"could not start {argv[0]!r}: {exc}", mask)
        step.stderr_tail = _tail(str(exc), mask)
        return step

    step.duration_s = time.monotonic() - start
    step.exit_code = proc.returncode
    step.stdout_tail = captured_tail(proc.stdout)
    step.stderr_tail = captured_tail(proc.stderr)
    return step


def _status(agent: StepResult, verifier: StepResult, changed: list[str]) -> str:
    """Verdict comes from process exits only. Agent prose is never a signal."""
    if agent.timed_out:
        return "agent_timeout"
    if agent.error:
        return "agent_error"
    if agent.exit_code != 0:
        return "agent_failed"
    if verifier.timed_out:
        return "verifier_timeout"
    if verifier.error:
        return "verifier_error"
    if verifier.exit_code != 0:
        return "verifier_failed"
    if not changed:
        return "no_changes"
    return STATUS_OK


def preflight_run(
    repo: Path | str,
    agent_argv: Sequence[str],
    verify_argv: Sequence[str],
    timeout: float = 900.0,
    runs_root: Path | str | None = None,
    pass_env: Sequence[str] = (),
) -> dict[str, Any]:
    """Check everything :func:`run` needs, without cloning or running anything."""
    checks: list[dict[str, Any]] = []
    detail: dict[str, Any] = {}

    def attempt(name: str, check) -> None:
        try:
            note = check()
            checks.append({"name": name, "ok": True, "detail": note or ""})
        except (HxError, GitError, TypeError, ValueError) as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})

    def argv_check(label: str, argv: Sequence[str]):
        def check() -> str:
            detail[label] = _normalize_argv(argv, label)
            return " ".join(_env.redact_argv(detail[label]))

        return check

    def executable_check(label: str):
        def check() -> str:
            program = detail[label][0]
            found = shutil.which(program) or (program if Path(program).is_file() else None)
            if not found:
                raise HxError(f"{program!r} was not found on PATH")
            return str(found)

        return check

    def timeout_check() -> str:
        seconds = float(timeout)
        if not math.isfinite(seconds) or seconds <= 0:
            raise HxError(f"timeout must be a positive number of seconds, got {timeout!r}")
        return f"{seconds:g} s for each process"

    def pass_env_check() -> str:
        return ", ".join(_env.validate_pass_env(pass_env)) or "none (default allowlist only)"

    def source_check() -> str:
        detail["source"], base = _prepare_source(repo)
        return f"clean at {base[:12]}"

    def runs_root_check() -> str:
        root = (Path(runs_root).expanduser() if runs_root else default_runs_root()).resolve()
        source = detail.get("source")
        if source is None:
            raise HxError("cannot be judged without a valid source repository")
        if root == source or source in root.parents:
            raise HxError("runs_root must be outside the source repository")
        return str(root)

    def git_check() -> str:
        version = _git.git_version()
        if version is None or version < _git.MIN_GIT_VERSION:
            needed = ".".join(map(str, _git.MIN_GIT_VERSION))
            raise HxError(f"hx accept needs git {needed} or newer")
        return ".".join(map(str, version))

    attempt("agent_argv", argv_check("agent", agent_argv))
    attempt("verifier_argv", argv_check("verifier", verify_argv))
    attempt("timeout", timeout_check)
    attempt("pass_env", pass_env_check)
    attempt("source_repo", source_check)
    attempt("runs_root", runs_root_check)
    if "agent" in detail:
        attempt("agent_executable", executable_check("agent"))
    if "verifier" in detail:
        attempt("verifier_executable", executable_check("verifier"))
    attempt("git_version", git_check)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def _prepare_source(repo: Path | str) -> tuple[Path, str]:
    """The resolved repository root and its HEAD, or an error if a run cannot start."""
    source = Path(repo).expanduser()
    if not source.is_dir():
        raise HxError(f"source path is not a directory: {source}")
    source = source.resolve()
    top = _git.work_tree_root(source)
    if top is None:
        raise HxError(f"not a git work tree: {source}")
    if top != source:
        raise HxError(
            f"{source} is a subdirectory of the git work tree at {top}: hx runs on the "
            "repository root so the isolated clone covers the whole checkout"
        )
    try:
        base = _git.head_sha(source)
    except GitError as exc:
        raise HxError(f"{source} has no commit to base the run on: {exc}") from exc
    dirty = _git.dirty_paths(source)
    if dirty:
        raise HxError("source repository must be clean before a run")
    return source, base


def run(
    repo: Path | str,
    agent_argv: Sequence[str],
    verify_argv: Sequence[str],
    timeout: float = 900.0,
    runs_root: Path | str | None = None,
    pass_env: Sequence[str] = (),
) -> RunResult:
    """Delegate one task to ``agent_argv`` in a throwaway clone, then verify it.

    Args:
        repo: path to your git repository. Only read, never written, by this call.
        agent_argv: program + arguments for the coding agent, executed with
            ``cwd`` set to the clone. Never a shell string.
        verify_argv: program + arguments for the verifier, run in the same clone.
            A non-empty argv is mandatory: no verifier, no verdict.
        timeout: seconds allowed for *each* of the two processes.
        runs_root: where the clone and the record go. Defaults to
            :func:`default_runs_root`, always outside ``repo``.
        pass_env: names of extra environment variables to forward (names only).

    Returns:
        A :class:`RunResult`. The patch it points at is pending human review:
        nothing is applied until :func:`accept` is called explicitly.
    """
    agent_cmd = _normalize_argv(agent_argv, "agent")
    verifier_cmd = _normalize_argv(verify_argv, "verifier")
    try:
        seconds = float(timeout)
        names = _env.validate_pass_env(pass_env)
    except (TypeError, ValueError) as exc:
        raise HxError(str(exc)) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise HxError(f"timeout must be a positive number of seconds, got {timeout!r}")

    source, base = _prepare_source(repo)
    dirty: list[str] = []  # a run only starts on a clean source

    root = (Path(runs_root).expanduser() if runs_root else default_runs_root()).resolve()
    if root == source or source in root.parents:
        raise HxError("runs_root must be outside the source repository")
    run_id = _new_run_id()
    run_dir = root / run_id
    attempt = 0
    while run_dir.exists():
        attempt += 1
        run_dir = root / f"{run_id}-{attempt}"
    run_id = run_dir.name
    work_dir = run_dir / "work"
    home_dir = run_dir / "home"
    tmp_dir = run_dir / "tmp"
    for path in (run_dir, home_dir, tmp_dir):
        path.mkdir(parents=True, exist_ok=True)

    try:
        _git.clone(source, work_dir, base)
    except GitError as exc:
        raise HxError(f"could not create the isolated clone: {exc}") from exc

    env, env_names = _env.isolated_env(names, home=home_dir, tmp=tmp_dir)
    mask = _env.secret_values(names)

    agent_step = _run_step(agent_cmd, work_dir, env, seconds, mask)
    before_verification: bytes | None = None
    measure_error: str | None = None
    if not (agent_step.error or agent_step.timed_out or agent_step.exit_code != 0):
        try:
            _git.stage_all(work_dir)
            before_verification = _git.staged_patch(work_dir, base)
        except GitError as exc:
            measure_error = str(exc)

    if before_verification is None:
        verifier_step = StepResult(
            argv=[_env.mask_values(arg, mask) for arg in verifier_cmd], skipped=True
        )
        verifier_step.error = (
            "skipped: could not capture the proposed patch"
            if measure_error
            else "skipped: agent did not exit with code 0"
        )
    else:
        verifier_step = _run_step(verifier_cmd, work_dir, env, seconds, mask)

    changed: list[str] = []
    patch = b""
    try:
        _git.stage_all(work_dir)
        changed = _git.staged_names(work_dir, base)
        patch = _git.staged_patch(work_dir, base)
    except GitError as exc:
        # The agent may have damaged its own clone. The source tree is unaffected.
        measure_error = str(exc)

    status = (
        _status(agent_step, verifier_step, changed) if measure_error is None else "workspace_lost"
    )
    if status in (STATUS_OK, "no_changes") and before_verification != patch:
        status = "verifier_changed_patch"
    patch_path = run_dir / PATCH_NAME
    patch_path.write_bytes(patch)
    digest = hashlib.sha256(patch).hexdigest()

    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "tool": "hx",
        "hx_version": VERSION,
        "run_id": run_id,
        "created_at": _now(),
        "source_repo": str(source),
        "base_commit": base,
        "source_clean_at_run": not dirty,
        "source_dirty_files_at_run": dirty,
        "runs_root": str(root.resolve()),
        "run_dir": str(run_dir.resolve()),
        "work_dir": str(work_dir.resolve()),
        "home_dir": str(home_dir.resolve()),
        "agent": agent_step.to_dict(),
        "verifier": verifier_step.to_dict(),
        "env_names": env_names,
        "pass_env_names": list(names),
        "status": status,
        "passed": status == STATUS_OK,
        "changed_files": changed,
        "change_count": len(changed),
        "patch_file": PATCH_NAME,
        "patch_sha256": digest,
        "verified_patch_sha256": digest if status == STATUS_OK else None,
        "patch_bytes": len(patch),
        "measure_error": measure_error,
        "accept": {"accepted": False},
        "isolation": {
            "clone": "fresh local clone at base_commit, push disabled",
            "agent_home": "private HOME and temp dir inside run_dir",
            "environment": "allowlist of variable names; pass_env names only",
            "os_sandbox": False,
            "note": (
                "git-level isolation only: not an OS security boundary. The agent "
                "runs as you and can touch what you can touch. hx itself writes back "
                "only an explicitly accepted patch."
            ),
        },
    }
    _write_record(run_dir, record)

    return RunResult(
        run_id=run_id,
        run_dir=run_dir,
        work_dir=work_dir,
        home_dir=home_dir,
        source_repo=source,
        base_commit=base,
        source_clean=not dirty,
        source_dirty_files=dirty,
        agent=agent_step,
        verifier=verifier_step,
        status=status,
        passed=record["passed"],
        changed_files=changed,
        patch_path=patch_path,
        patch_sha256=digest,
        patch_bytes=len(patch),
        env_names=env_names,
        pass_env_names=list(names),
        created_at=record["created_at"],
        record=record,
    )


def _write_record(run_dir: Path, record: dict[str, Any]) -> None:
    (run_dir / RECORD_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def resolve_run(
    run_ref: Path | str, runs_root: Path | str | None = None
) -> tuple[Path, dict[str, Any]]:
    """Find a run directory by id (or by path) and load its record."""
    candidate = Path(str(run_ref)).expanduser()
    if not (candidate.is_dir() and (candidate / RECORD_NAME).is_file()):
        root = Path(runs_root).expanduser() if runs_root else default_runs_root()
        candidate = root / str(run_ref)
    if not (candidate.is_dir() and (candidate / RECORD_NAME).is_file()):
        raise HxError(
            f"no hx run found for {run_ref!r}; look under {default_runs_root()} or pass --runs-root"
        )
    try:
        record = json.loads((candidate / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HxError(f"cannot read the run record in {candidate}: {exc}") from exc
    return candidate, record


def inspect_run(run_ref: Path | str, runs_root: Path | str | None = None) -> dict[str, Any]:
    """The persisted record of a run: exits, timings, files, digest, accept state."""
    _, record = resolve_run(run_ref, runs_root)
    return record


def check_patch_digest(run_dir: Path, record: dict[str, Any]) -> bytes:
    if record.get("patch_file", PATCH_NAME) != PATCH_NAME:
        raise AcceptanceRefused("unexpected patch filename")
    raw = (run_dir / PATCH_NAME).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != record.get("patch_sha256"):
        raise AcceptanceRefused(
            "patch digest mismatch: the recorded sha256 is "
            f"{record.get('patch_sha256')} but {PATCH_NAME} now hashes to {digest}. "
            "The patch file was modified after the run: refusing to apply it."
        )
    return raw


def patch_text(run_ref: Path | str, runs_root: Path | str | None = None) -> str:
    """The recorded patch, digest-checked, as text (for reading and reviewing)."""
    run_dir, record = resolve_run(run_ref, runs_root)
    return check_patch_digest(run_dir, record).decode("utf-8", errors="replace")


def _check_git_version() -> None:
    version = _git.git_version()
    if version is None or version < _git.MIN_GIT_VERSION:
        needed = ".".join(map(str, _git.MIN_GIT_VERSION))
        found = ".".join(map(str, version)) if version else "unknown"
        raise AcceptanceRefused(f"accept needs git {needed} or newer (found {found})")


def _check_patch_paths(source: Path, patch: bytes, changed: object) -> None:
    """Refuse a patch that touches anything outside the recorded change set."""
    allowed = set(changed) if isinstance(changed, list) else set()
    if _git.has_rename_or_copy(patch):
        raise AcceptanceRefused("the patch contains a rename or copy, which hx never records")
    try:
        paths = _git.patch_paths(source, patch)
    except GitError as exc:
        raise AcceptanceRefused(str(exc)) from exc
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        unsafe = (
            path.startswith(("/", "\\"))
            or (len(path) > 1 and path[1] == ":")
            or any(part in ("", "..") or part.lower() == ".git" for part in parts)
        )
        if unsafe:
            raise AcceptanceRefused(f"the patch touches an unsafe path: {path!r}")
        if path not in allowed:
            raise AcceptanceRefused(
                f"the patch touches {path!r}, which is not in the recorded changed files"
            )


def accept(run_ref: Path | str, runs_root: Path | str | None = None) -> dict[str, Any]:
    """Apply a reviewed patch to its source repository. The only write hx performs.

    Refuses (and writes nothing) unless every one of these holds:

    * the run was never accepted before;
    * ``patch.diff`` still matches the recorded sha256;
    * verification actually passed (status ``ok``: no failure, no timeout);
    * the patch is not empty;
    * the target HEAD is still the commit the run was based on;
    * the source was clean at run time and the target is still clean;
    * ``git apply --check`` succeeds.

    Even then hx only runs ``git apply``: it never stages, commits or pushes.
    """
    run_dir, record = resolve_run(run_ref, runs_root)
    if record.get("accept", {}).get("accepted"):
        raise AcceptanceRefused(
            f"run {record.get('run_id')} was already accepted at {record['accept'].get('accepted_at')}"
        )
    patch = check_patch_digest(run_dir, record)

    if not patch and record.get("status") == "no_changes":
        raise AcceptanceRefused("empty patch: there is nothing to apply")

    status = str(record.get("status"))
    if not record.get("passed") or status != STATUS_OK:
        details = []
        if record.get("agent", {}).get("timed_out"):
            details.append("the agent timed out")
        if record.get("verifier", {}).get("timed_out"):
            details.append("the verifier timed out")
        if record.get("verifier", {}).get("skipped"):
            details.append("the verifier never ran")
        if record.get("measure_error"):
            details.append(f"the diff could not be measured ({record['measure_error']})")
        suffix = f" ({'; '.join(details)})" if details else ""
        raise AcceptanceRefused(
            f"verification did not pass (status={status}){suffix}: accept only runs on status={STATUS_OK}"
        )
    if not patch:
        raise AcceptanceRefused("empty patch: there is nothing to apply")
    if record.get("verified_patch_sha256") != record.get("patch_sha256"):
        raise AcceptanceRefused("run lacks patch verification evidence; rerun with this hx version")

    source = Path(str(record.get("source_repo")))
    if not source.is_dir() or not _git.is_work_tree(source):
        raise AcceptanceRefused(f"target repository is gone or is not a work tree: {source}")
    head = _git.head_sha(source)
    if head != record.get("base_commit"):
        raise AcceptanceRefused(
            f"target HEAD changed since the run (was {record.get('base_commit')}, now {head}): "
            "the patch was measured against a different commit, run hx again"
        )
    now_dirty = _git.dirty_paths(source)
    if (
        now_dirty
        or not record.get("source_clean_at_run")
        or record.get("source_dirty_files_at_run")
    ):
        raise AcceptanceRefused(
            "target clean status changed since the run: "
            f"at run time {record.get('source_dirty_files_at_run')}, now {now_dirty}"
        )

    _check_git_version()
    _check_patch_paths(source, patch, record.get("changed_files"))
    rc, _out, err = _git.apply_patch(source, patch, check_only=True)
    if rc != 0:
        raise AcceptanceRefused(f"git apply --check refused the patch: {err.strip()[:600]}")
    rc, _out, err = _git.apply_patch(source, patch, check_only=False)
    if rc != 0:
        raise AcceptanceRefused(f"git apply failed: {err.strip()[:600]}")

    record["accept"] = {
        "accepted": True,
        "accepted_at": _now(),
        "patch_sha256": record.get("patch_sha256"),
        "applied_files": record.get("changed_files", []),
        "committed": False,
        "pushed": False,
    }
    _write_record(run_dir, record)
    return record["accept"]
