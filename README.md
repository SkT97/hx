# hx

Run a coding agent in a fresh local Git clone, verify its work, and inspect the patch before choosing whether to apply it. hx records the command exits, changed files, and patch digest.

## Quick start

Requires Python 3.12+, Git and [uv](https://docs.astral.sh/uv/). From this repository:

```sh
uv run --locked hx demo
```

The offline demo creates a disposable Git fixture, blocks an agent that only claims success, verifies a real fix and explicitly applies it to that fixture. It uses a deterministic fake agent; no model, API key or existing repository is involved.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wave.svg">
  <img src="assets/wave.svg" alt="Section divider">
</picture>

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/workflow.svg">
  <img src="assets/workflow.svg" alt="A coding agent works in a local clone. Verification clears a patch for review and explicit acceptance.">
</picture>

A failed verifier blocks acceptance. A passing verifier leaves a patch for you to inspect and apply explicitly. Git clone isolation does not sandbox the agent.

<details>
<summary>Run your own agent and review a patch</summary>

```sh
uv run --locked hx run /path/to/repo --timeout 900 -- my-agent "fix the failing test" -- python -m pytest -q
uv run --locked hx inspect RUN_ID --json
uv run --locked hx diff RUN_ID
uv run --locked hx accept RUN_ID --yes
```

The two `--` separators delimit argument vectors. Commands are executed without a shell. The source must be clean and the run directory must be outside it. Pass an environment variable deliberately with `--pass-env NAME`; the default environment is allowlisted, with private HOME and temporary directories.

`accept` refuses failed or timed-out verification, an empty or modified patch, a changed source HEAD, and a dirty target. It applies an uncommitted patch; you own staging, commits and publication.

</details>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wave.svg">
  <img src="assets/wave.svg" alt="Section divider">
</picture>

## Verification

The recorded local suite covers failed verification, timeouts, changed source HEAD, and patch integrity. See [verification](docs/verification.md).

<details>
<summary>Local verification and implementation choices</summary>

Checked locally on Windows, 16 November 2026. The test suite covers source fingerprints before/after runs, verifier failures, timeout cleanup including a child holding output pipes, new-file patches, changed HEAD, dirty targets, digest mismatches and forwarded-value redaction. See [verification](docs/verification.md) for executed commands and limits.

The demo is a correctness check of the delegation loop. It is not a coding-model benchmark or a claim of improved task success.

- Fresh clones use `--no-hardlinks` to avoid sharing the source object files. Push is disabled in clone configuration. See [Git clone](https://git-scm.com/docs/git-clone).
- Argument vectors, explicit environments and process lifetimes follow the [Python subprocess API](https://docs.python.org/3.12/library/subprocess.html).
- Windows cleanup uses a suspended process assigned to a [Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects). POSIX cleanup targets a new process group.
- Runtime code uses the standard library; `uv.lock` pins development tooling. CI is configured to run tests, lint and the offline demo on Linux and Windows.

Sources reviewed 16 November 2026. These are design references, not external certification.

</details>

<details>
<summary>Development checks</summary>

```sh
uv sync --locked --group dev
uv run --locked python -m unittest discover -s tests -t .
uv run --locked ruff check .
uv run --locked ruff format --check .
```

</details>

## Limits

Git isolation is **not an OS security boundary**. An agent running as you can access your files and network, change its clone configuration, or tamper with a verifier. Use an OS/container boundary and a trusted external verifier for untrusted agents.

On POSIX, a descendant that creates a new session can leave the tracked process group. Output tails are bounded in the record, but subprocess output is buffered in memory. Known forwarded values are masked; this is not a general secret detector, and the patch may contain sensitive content. Inspect and scan artifacts before sharing them.

A digest detects accidental patch changes relative to the local record; someone who can change both can replace both. Acceptance is a local workflow guard, not a signed attestation or concurrent-writer transaction.

<details>
<summary>A patch in the margins · 97</summary>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/side-quest.svg">
  <img src="assets/side-quest.svg" alt="A little diff reveals the profile motto.">
</picture>

</details>

## License

[Apache-2.0](LICENSE).
