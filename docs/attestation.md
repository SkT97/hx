# Checking a run and attesting it

`hx check RUN` recomputes what the files of a run allow. It exits 1 if any check fails.

| Check | What it recomputes |
| --- | --- |
| `patch_file_name` | The record names `patch.diff`. |
| `patch_digest` | Size and SHA-256 of `patch.diff` against the record. |
| `verified_digest` | The verified digest equals the patch digest when the status is `ok`, and is empty otherwise. |
| `passed_flag` | `passed` is true exactly when the status is `ok`. |
| `status_from_exits` | The status implied by the recorded agent and verifier exits equals the recorded status. |
| `changed_files_match_patch` | The paths read from the patch by `git apply --numstat` equal the recorded file list. |

These checks catch a stale or hand-edited record. They do not stop someone who
rewrites the patch, the digest and the exit codes together: the files are
writable by the user who ran the agent.

## Statement

`hx attest RUN` prints an unsigned [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md)
for a run that passes every check. It refuses otherwise.

- `subject`: the patch, named `patch.diff`, with its SHA-256.
- `predicateType`: the address of this page.
- `predicate`: hx version, run id, creation time, base commit, whether the
  source was clean, status, `passed`, agent and verifier argv with exit code,
  timeout flag and duration, changed files, patch size, and `os_sandbox`.

The predicate leaves out local paths and the captured output of both commands.
Argv values are redacted as in the record.

## Signing

hx does not sign. A signature adds trust only when the key is out of the
agent's reach, so sign the statement with a DSSE-capable tool on a machine or
account the agent cannot read, and verify the signature before relying on the
statement.
