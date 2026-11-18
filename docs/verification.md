# Verification

Checked locally on Windows on 16 November 2026, using Python 3.12.14 and the checked-in `uv.lock`.

```sh
uv sync --locked --group dev
uv run --locked python -m unittest discover -s tests -t .
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked hx demo
```

The GitHub Actions workflow is prepared for Linux and Windows. No GitHub run is claimed before a repository is created and the workflow executes. Local verification is not a hosted CI result.

Local suite: **43 tests passed**. Ruff checks and formatting passed; the documented demo exited successfully. Markdown lint passed.
