# Exit codes, result and preflight

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The command succeeded. For `hx run`, verification passed. |
| 1 | An error or a refusal: invalid input, a failed preflight or check, or `accept` refused to write. |
| 2 | Usage error: the command line itself is wrong. |
| 3 | `hx run` finished but did not pass: see the status. |
| 130 | Interrupted. |

## `hx run --json`

With `--json`, `hx run` prints one object instead of text and exits with the
code above. `schema` is `hx.result/1`. A field is never removed or retyped within
a schema version.

| Field | Meaning |
| --- | --- |
| `run_id`, `run_dir` | The run and the local directory that holds its record and patch. |
| `status` | One of the statuses below. |
| `passed` | True exactly when the status is `ok`. |
| `exit_code` | The exit code of this command: 0 when `passed`, otherwise 3. |
| `base_commit` | The commit the run started from. |
| `changed_files`, `patch_sha256`, `patch_bytes` | What the agent changed, as recorded. |
| `agent`, `verifier` | `exit_code`, `timed_out` and `duration_s` of each process. |

| Status | Meaning |
| --- | --- |
| `ok` | Both processes exited 0 and the patch is not empty. |
| `no_changes` | Both exited 0 but the agent changed nothing. |
| `agent_timeout`, `agent_error`, `agent_failed` | The agent timed out, did not start, or exited non-zero. |
| `verifier_timeout`, `verifier_error`, `verifier_failed` | The same for the verifier. |
| `workspace_lost` | The patch could not be measured. |
| `verifier_changed_patch` | The verifier modified the patch, so acceptance is blocked. |

## `hx run --preflight`

`hx run REPO --preflight -- <agent argv> -- <verify argv>` checks what a run
needs and runs nothing: no clone, no agent, no verifier. It exits 0 when every
check passes and 1 otherwise. With `--json` it prints `hx.preflight/1` with the
same checks.

| Check | What it verifies |
| --- | --- |
| `agent_argv`, `verifier_argv` | Each command is non-empty. Secret-looking values are redacted in the output. |
| `timeout` | A positive, finite number of seconds. |
| `pass_env` | The names passed with `--pass-env` are valid. |
| `source_repo` | The repository root is a clean Git work tree with a commit. |
| `runs_root` | The run directory is outside the repository. |
| `agent_executable`, `verifier_executable` | The program is on `PATH` or exists as a file. |
| `git_version` | Git is 2.32 or newer, which `hx accept` needs. |
