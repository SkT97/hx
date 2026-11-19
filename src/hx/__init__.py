"""hx - verifier-first delegation for coding agents.

You keep the repository. The agent works in a throwaway clone; a verifier you
choose decides whether the work is worth anything; the result is a patch that
sits pending until *you* run ``accept``.

    import hx

    res = hx.run(
        "/path/to/any/repo",
        ["my-agent", "--task", "make the tests pass"],
        ["python", "-m", "pytest", "-q"],
        timeout=900,
    )
    print(res.status, res.changed_files, res.patch_sha256)
    print(hx.patch_text(res.run_id))          # review it
    # hx.accept(res.run_id)                   # only you, explicitly

Isolation is git-level (fresh clone, push disabled, private HOME, allowlisted
environment) plus human review. It is **not** an OS security boundary.
"""

from ._attest import attest_run, check_run
from ._core import (
    AcceptanceRefused,
    HxError,
    RunResult,
    StepResult,
    accept,
    default_runs_root,
    inspect_run,
    patch_text,
    preflight_run,
    run,
)
from ._demo import demo

__version__ = "0.1.0"

__all__ = [
    "AcceptanceRefused",
    "HxError",
    "RunResult",
    "StepResult",
    "__version__",
    "accept",
    "attest_run",
    "check_run",
    "default_runs_root",
    "demo",
    "inspect_run",
    "patch_text",
    "preflight_run",
    "run",
]
