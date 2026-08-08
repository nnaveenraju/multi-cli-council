# Contributing

Thanks for your interest in contributing to Multi-CLI Council!

## Development setup

Requirements: Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/nnaveenraju/multi-cli-council.git
cd multi-cli-council
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

To actually run the pipeline you also need one or more logged-in provider CLIs
on your `PATH` (`claude`, `grok`, `kimi`, `agy`), but **the test suite does not
require them** — model invocations are mocked.

## Running checks

```bash
pytest                 # test suite
ruff check src tests   # lint
```

Please make sure both pass before opening a PR.

## Project conventions

- Source lives in `src/council/`; CLI entry point is `council.cli:app` (typer).
- Stage prompts live in `prompts/` and can be edited without code changes.
- Default config is `config.yaml`; use `config.local.yaml` for your own
  overrides (gitignored — never commit secrets, API keys, or tokens).
- Line length is 100 (`ruff` config in `pyproject.toml`).
- Runtime data goes under `data/sessions/` (gitignored).

## Making changes

- Keep changes scoped and minimal — a tidy, reviewable diff beats an
  opportunistic cleanup.
- Add tests for new behavior; the suite already covers config, adapters,
  pipeline stages, export, and images (`tests/`).
- If you change user-facing commands or config keys, update `README.md`.
- If you add or rename a stage prompt, update the prompts list in the
  README's Configuration section.

## Pull requests

1. Fork the repo and create a branch from `main`.
2. Make your change with tests and docs updated.
3. Fill in the PR template — what changed, why, how it was tested.
4. Keep PRs focused: one feature or fix per PR.

## Reporting issues

Use the GitHub issue templates (bug report / feature request). For bugs,
include the command you ran, the relevant `raw_log.txt` /
`events.jsonl` excerpts from the session dir, and the output of
`council doctor`.

## Code of conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).
