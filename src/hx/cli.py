"""``hx`` command line: run, inspect, diff, accept, check, attest, demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__, _git
from ._attest import attest_run, check_run
from ._core import HxError, accept, default_runs_root, inspect_run, patch_text, preflight_run
from ._core import run as core_run
from ._demo import demo

USAGE = """hx {version} - verifier-first delegation for coding agents

usage:
  hx run REPO [--timeout SEC] [--runs-root DIR] [--pass-env NAME]... [--preflight] [--json] \\
       -- <agent argv...> -- <verify argv...>
  hx inspect RUN [--runs-root DIR] [--json]
  hx diff    RUN [--runs-root DIR]
  hx accept  RUN --yes [--runs-root DIR]
  hx check   RUN [--runs-root DIR]
  hx attest  RUN [--runs-root DIR]
  hx demo    [--runs-root DIR] [--keep]
  hx --version | --help

The two `--` separators are what keep argv shell-free: everything between the
first and the second `--` is the agent command, everything after the second is
the verifier command. No shell ever sees them.

Examples:
  hx run . --timeout 900 -- my-agent "make tests pass" -- python -m pytest -q
  hx run ~/src/app --pass-env CI -- my-agent --task fix-bug -- npm test
  hx demo

hx never commits and never pushes. `hx accept` is the only command that writes
to your repository, and it refuses unless verification passed, the patch digest
matches and your target still has the HEAD and cleanliness it had at run time.
"""


def _split_segments(tokens: list[str]) -> tuple[list[str], list[str], list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "--":
            segments.append([])
        else:
            segments[-1].append(token)
    if len(segments) != 3:
        raise HxError(
            "usage: hx run REPO [options] -- <agent argv...> -- <verify argv...> "
            f"(expected two '--' separators, found {len(segments) - 1})"
        )
    for label, seg in (("agent", segments[1]), ("verifier", segments[2])):
        if not [t for t in seg if t.strip()]:
            raise HxError(f"the {label} argv is empty - hx needs an explicit {label} command")
    return segments[0], segments[1], segments[2]


def _step_line(label: str, step: dict[str, Any]) -> str:
    if step.get("skipped"):
        state = f"skipped ({step.get('error')})"
    elif step.get("timed_out"):
        state = f"TIMEOUT after {step.get('duration_s')}s"
    elif step.get("exit_code") is None:
        state = f"not started ({step.get('error')})"
    else:
        state = f"rc={step['exit_code']} in {step['duration_s']}s"
    return f"{label} {state}"


def _result_json(res) -> dict[str, Any]:
    """The versioned machine-readable result of ``hx run --json``."""
    record = res.record

    def step(key: str) -> dict[str, Any]:
        return {name: record[key][name] for name in ("exit_code", "timed_out", "duration_s")}

    return {
        "schema": "hx.result/1",
        "run_id": res.run_id,
        "status": res.status,
        "passed": res.passed,
        "exit_code": 0 if res.passed else 3,
        "base_commit": res.base_commit,
        "changed_files": res.changed_files,
        "patch_sha256": res.patch_sha256,
        "patch_bytes": res.patch_bytes,
        "agent": step("agent"),
        "verifier": step("verifier"),
        "run_dir": str(res.run_dir),
    }


def _cmd_run(tokens: list[str]) -> int:
    head, agent_argv, verify_argv = _split_segments(tokens)
    parser = argparse.ArgumentParser(prog="hx run", add_help=False)
    parser.add_argument("repo")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--runs-root", default=None)
    parser.add_argument("--pass-env", action="append", default=[], metavar="NAME")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args(head)

    if opts.preflight:
        report = preflight_run(
            opts.repo,
            agent_argv,
            verify_argv,
            timeout=opts.timeout,
            runs_root=opts.runs_root,
            pass_env=opts.pass_env,
        )
        if opts.json:
            print(json.dumps({"schema": "hx.preflight/1", **report}, indent=2, sort_keys=True))
        else:
            for item in report["checks"]:
                label = "ok  " if item["ok"] else "FAIL"
                print(f"{label} {item['name']}" + (f": {item['detail']}" if item["detail"] else ""))
        return 0 if report["ok"] else 1

    res = core_run(
        opts.repo,
        agent_argv,
        verify_argv,
        timeout=opts.timeout,
        runs_root=opts.runs_root,
        pass_env=opts.pass_env,
    )
    if opts.json:
        print(json.dumps(_result_json(res), indent=2, sort_keys=True))
        return 0 if res.passed else 3
    runs_root = str(Path(opts.runs_root).expanduser().resolve()) if opts.runs_root else None
    print(f"run_id={res.run_id}")
    print(f"source={res.source_repo} base={res.base_commit[:12]}")
    print(f"status={res.status}")
    print(_step_line("  agent   :", res.agent.to_dict()))
    print(_step_line("  verifier:", res.verifier.to_dict()))
    print(f"  changed : {len(res.changed_files)} file(s)")
    for name in res.changed_files[:50]:
        print(f"            {name}")
    print(f"  patch   : {res.patch_path}")
    print(f"  sha256  : {res.patch_sha256}")
    root = runs_root or default_runs_root()
    print(f"review it with: hx diff {res.run_id} --runs-root {root}")
    print(f"apply it with : hx accept {res.run_id} --yes --runs-root {root}")
    if res.status == "no_changes":
        print("note: the verifier passed but the agent changed nothing.")
    return 0 if res.passed else 3


def _cmd_inspect(tokens: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hx inspect", add_help=False)
    parser.add_argument("run")
    parser.add_argument("--runs-root", default=None)
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args(tokens)
    record = inspect_run(opts.run, runs_root=opts.runs_root)
    if opts.json:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    print(f"run_id     : {record['run_id']}")
    print(f"created_at : {record['created_at']}")
    print(f"source     : {record['source_repo']} @ {record['base_commit'][:12]}")
    print(
        "target at run time : "
        + ("clean" if record["source_clean_at_run"] else str(record["source_dirty_files_at_run"]))
    )
    print(f"status     : {record['status']} (passed={record['passed']})")
    print(_step_line("agent      :", record["agent"]))
    print(_step_line("verifier   :", record["verifier"]))
    print(f"changed    : {record['change_count']} file(s)")
    for name in record["changed_files"][:50]:
        print(f"             {name}")
    print(f"patch      : {Path(record['run_dir']) / record['patch_file']}")
    print(f"sha256     : {record['patch_sha256']} ({record['patch_bytes']} bytes)")
    print(f"env names  : {', '.join(record['env_names'])}")
    print(f"pass_env   : {', '.join(record['pass_env_names']) or '(none)'}")
    print(f"accept     : {record['accept']}")
    print(f"isolation  : {record['isolation']['note']}")
    return 0


def _cmd_diff(tokens: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hx diff", add_help=False)
    parser.add_argument("run")
    parser.add_argument("--runs-root", default=None)
    opts = parser.parse_args(tokens)
    sys.stdout.write(patch_text(opts.run, runs_root=opts.runs_root))
    return 0


def _cmd_accept(tokens: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hx accept", add_help=False)
    parser.add_argument("run")
    parser.add_argument("--runs-root", default=None)
    parser.add_argument("--yes", action="store_true")
    opts = parser.parse_args(tokens)
    record = inspect_run(opts.run, runs_root=opts.runs_root)
    print(f"target : {record.get('source_repo')} @ {str(record.get('base_commit'))[:12]}")
    for name in record.get("changed_files", [])[:50]:
        print(f"  {name}")
    if not opts.yes:
        print(
            "hx: refusing to write to your repository without --yes.\n"
            "      read the patch first: hx diff <run>, then: hx accept <run> --yes",
            file=sys.stderr,
        )
        return 2
    out = accept(opts.run, runs_root=opts.runs_root)
    print("applied (uncommitted) to the target working tree:")
    for name in out["applied_files"]:
        print(f"  {name}")
    print(f"sha256  : {out['patch_sha256']}")
    print(f"committed: {out['committed']}  pushed: {out['pushed']} - your move now.")
    return 0


def _cmd_check(tokens: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hx check", add_help=False)
    parser.add_argument("run")
    parser.add_argument("--runs-root", default=None)
    opts = parser.parse_args(tokens)
    report = check_run(opts.run, runs_root=opts.runs_root)
    for item in report["checks"]:
        label = "ok  " if item["ok"] else "FAIL"
        print(f"{label} {item['name']}" + (f": {item['detail']}" if item["detail"] else ""))
    return 0 if report["ok"] else 1


def _cmd_attest(tokens: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hx attest", add_help=False)
    parser.add_argument("run")
    parser.add_argument("--runs-root", default=None)
    opts = parser.parse_args(tokens)
    print(json.dumps(attest_run(opts.run, runs_root=opts.runs_root), indent=2, sort_keys=True))
    return 0


def _cmd_demo(tokens: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hx demo", add_help=False)
    parser.add_argument("--runs-root", default=None)
    parser.add_argument("--keep", action="store_true")
    opts = parser.parse_args(tokens)
    return demo(runs_root=opts.runs_root, keep=opts.keep)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (never touches a repo implicitly)."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens or tokens[0] in ("-h", "--help", "help"):
        print(USAGE.format(version=__version__))
        return 0
    if tokens[0] in ("-V", "--version"):
        print(f"hx {__version__}")
        return 0
    command, rest = tokens[0], tokens[1:]
    handlers = {
        "run": _cmd_run,
        "inspect": _cmd_inspect,
        "diff": _cmd_diff,
        "accept": _cmd_accept,
        "check": _cmd_check,
        "attest": _cmd_attest,
        "demo": _cmd_demo,
    }
    if command not in handlers:
        print(f"hx: unknown command {command!r}\n", file=sys.stderr)
        print(USAGE.format(version=__version__), file=sys.stderr)
        return 2
    try:
        if command == "demo":
            _git.git(["--version"])  # fail early and clearly if git is missing
        return handlers[command](rest)
    except HxError as exc:
        print(f"hx: error: {exc}", file=sys.stderr)
        return 2 if str(exc).startswith("usage:") else 1
    except KeyboardInterrupt:  # pragma: no cover
        print("hx: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
