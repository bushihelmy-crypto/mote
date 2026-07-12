# Contributing to mote

Thanks for your interest in contributing! This document covers the local setup,
coding conventions, and the checks your change must pass.

## Development setup

```bash
git clone <repo-url> mote
cd mote
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium   # only needed for browser-tool tests
pre-commit install
```

The repository root **is** the top-level `mote` package (`package_dir={"mote": "."}`),
so all imports are absolute: `from mote.<subpackage> import ...`.

## Running the tests

Tests live under `ztest/<subsystem>/` (not `tests/`).

```bash
pytest                       # full suite (config in pyproject.toml)
pytest ztest/roles ztest/loop ztest/executor   # a subset
```

## Linting & formatting

All formatting/lint runs through `pre-commit` (isort → black → flake8 → pyright):

```bash
pre-commit run --all-files
```

- Line width is **120** everywhere (`ruff.toml`, black, `isort --profile=black`).
- `pyright` type-checks changed files; `flake8`/`pyright` exclude `ztest/`.

## Architecture & layering discipline

mote is strictly layered (see `AGENTS.md` and `zdocs/ARCHITECTURE.md`):

```
common ◀ context/executor/router/session ◀ parser/think/loop ◀ roles ◀ environment ◀ cli
```

Dependencies point **downward only**. Cross-layer access goes through the
Protocols in `common/interface/` — never import an upper layer directly.

- **Human-facing text** is localized via `common/i18n/` (zh/en).
- **Model-facing text** (prompts, tool output, `<system-reminder>`) stays plain
  English and must not be routed through i18n.

## Third-party code

If you port, adapt, or vendor code from another project, add its attribution to
`NOTICE` and drop the upstream license text into `zthird_party_licenses/`.

## Pull requests

1. Create a topic branch off `main`.
2. Make your change with tests; keep commits focused.
3. Ensure `pre-commit run --all-files` and `pytest` are green.
4. Open a PR describing the *why*, not just the *what*.

By contributing you agree that your contributions are licensed under the MIT
License (see `LICENSE`).
