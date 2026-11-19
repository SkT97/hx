"""Offline checks of a recorded run, and an in-toto statement describing it.

``check_run`` recomputes what can be recomputed from the files of a run: the
patch digest, the status implied by the recorded exit codes, and the file list
implied by the patch. ``attest_run`` wraps a checked record in an in-toto
Statement v1 (https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md).

The statement is not signed. The record is a file the agent's user can write, so
a signature only means something when it is made outside the agent's reach.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import _git
from ._core import PATCH_NAME, STATUS_OK, HxError, StepResult, _status, resolve_run

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://github.com/SkT97/hx/blob/main/docs/attestation.md"
_NOT_DERIVABLE = ("workspace_lost", "verifier_changed_patch")


def _step(record: dict[str, Any], key: str) -> StepResult:
    data = record.get(key) or {}
    return StepResult(
        argv=list(data.get("argv") or []),
        exit_code=data.get("exit_code"),
        timed_out=bool(data.get("timed_out")),
        skipped=bool(data.get("skipped")),
        error=data.get("error"),
    )


def check_run(run_ref: Path | str, runs_root: Path | str | None = None) -> dict[str, Any]:
    """Recompute the evidence of a run and report each check."""
    run_dir, record = resolve_run(run_ref, runs_root)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    status = record.get("status")
    add("patch_file_name", record.get("patch_file") == PATCH_NAME, str(record.get("patch_file")))

    patch = b""
    patch_path = run_dir / PATCH_NAME
    try:
        size = patch_path.stat().st_size
        if size != record.get("patch_bytes"):
            add(
                "patch_digest", False, f"{size} bytes on disk, {record.get('patch_bytes')} recorded"
            )
        else:
            patch = patch_path.read_bytes()
            digest = hashlib.sha256(patch).hexdigest()
            add("patch_digest", digest == record.get("patch_sha256"), f"sha256 {digest}")
    except OSError as exc:
        add("patch_digest", False, f"cannot read {PATCH_NAME}: {exc}")

    expected = record.get("patch_sha256") if status == STATUS_OK else None
    add("verified_digest", record.get("verified_patch_sha256") == expected)
    add("passed_flag", bool(record.get("passed")) == (status == STATUS_OK), f"status {status}")

    derived = _status(
        _step(record, "agent"), _step(record, "verifier"), list(record.get("changed_files") or [])
    )
    add(
        "status_from_exits",
        status == derived or status in _NOT_DERIVABLE,
        f"recorded {status}, exits imply {derived}",
    )

    try:
        in_patch = sorted(_git.patch_paths(run_dir, patch)) if patch else []
        recorded = sorted(record.get("changed_files") or [])
        if _git.has_rename_or_copy(patch):
            add("changed_files_match_patch", False, "the patch contains a rename or copy")
        else:
            add("changed_files_match_patch", in_patch == recorded, f"{len(in_patch)} in the patch")
    except _git.GitError as exc:
        add("changed_files_match_patch", False, str(exc))

    return {
        "run_id": record.get("run_id"),
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }


def attest_run(run_ref: Path | str, runs_root: Path | str | None = None) -> dict[str, Any]:
    """An unsigned in-toto Statement v1 for a run whose record passes every check."""
    report = check_run(run_ref, runs_root)
    if not report["ok"]:
        bad = ", ".join(c["name"] for c in report["checks"] if not c["ok"])
        raise HxError(f"integrity check failed ({bad}): no statement is produced")
    _, record = resolve_run(run_ref, runs_root)

    def step(key: str) -> dict[str, Any]:
        data = record[key]
        return {
            name: data[name] for name in ("argv", "exit_code", "timed_out", "skipped", "duration_s")
        }

    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": PATCH_NAME, "digest": {"sha256": record["patch_sha256"]}}],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "hx_version": record["hx_version"],
            "run_id": record["run_id"],
            "created_at": record["created_at"],
            "base_commit": record["base_commit"],
            "source_clean_at_run": record["source_clean_at_run"],
            "status": record["status"],
            "passed": record["passed"],
            "agent": step("agent"),
            "verifier": step("verifier"),
            "changed_files": record["changed_files"],
            "patch_bytes": record["patch_bytes"],
            "os_sandbox": record["isolation"]["os_sandbox"],
        },
    }
