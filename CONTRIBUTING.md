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
contracts <- kernel <- runtime <- orchestration <- product
```

Dependencies point **downward only**. Cross-layer access goes through the
Protocols in `contracts/ports/` - never import an upper layer directly.

- **Human-facing text** is localized via `product/i18n/` (zh/en).
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

## Releasing (maintainers)

mote follows [SemVer](https://semver.org/) and publishes to
[PyPI](https://pypi.org/p/mote) via the `.github/workflows/publish.yml`
workflow, which triggers on `v*` tag pushes.

### One-time setup

PyPI publishing uses **OIDC trusted publishing** (no API token stored in the
repo). Before the first release a PyPI owner must register this repo as a
trusted publisher at <https://pypi.org/manage/project/mote/settings/publishing/>:

- **Owner / repository**: the GitHub `org/repo`
- **Workflow name**: `publish.yml`
- **Environment name**: `pypi`

The workflow's `publish` job runs in the `pypi` GitHub Environment and requests
`id-token: write`; no other secret is needed.

### Cutting a release

1. Pick the new version `X.Y.Z` (major = breaking, minor = feature, patch = fix).
2. Bump `version` in `pyproject.toml`.
3. Move the `## [Unreleased]` entries in `CHANGELOG.md` under a new
   `## [X.Y.Z]` heading (keep an empty `Unreleased` section on top).
4. Commit the bump: `git commit -am "release: vX.Y.Z"`.
5. Ensure `pre-commit run --all-files` and `pytest` are green.
6. Tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin main --tags
   ```

The tag push runs `publish.yml`, which:

- builds the sdist + wheel and **asserts the runtime data files are packaged**
  (`config.example.yaml`, `product/routing/squilla/ml/router.runtime.yaml` — first-run bootstrap
  and the ML router read these, so a missing file fails the build);
- publishes to PyPI (skips already-uploaded files, so re-runs are safe);
- signs the artifacts with Sigstore and creates/updates the GitHub Release with
  auto-generated notes.

`workflow_dispatch` (Actions tab → *Publish to PyPI* → *Run workflow*) is the
escape hatch for re-publishing an existing tag.

### Verifying the build locally

To reproduce the packaging check the workflow runs:

```bash
python -m build --sdist --wheel
python -m twine check dist/*
python -m zipfile -l dist/*.whl | grep -E "config.example.yaml|router.runtime.yaml"
```
