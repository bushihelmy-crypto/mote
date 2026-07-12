# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Third-party attribution: `NOTICE` file plus a `zthird_party_licenses/`
  directory carrying the full upstream license texts for the projects mote
  ports, adapts, or vendors (codex, open-code-review, opensquilla, browser-use,
  codegraph, hermes-agent, langgraph, markitdown, marktext, CLI-Anything,
  agent-sandbox, oh-my-claudecode, and the vendored ripgrep binary).
- `pyproject.toml` (PEP 621): full project metadata, dependency list, `mote`
  console entry point, and pytest configuration.
- GitHub Actions CI (`.github/workflows/ci.yml`): pre-commit lint job and a
  pytest matrix across Python 3.10–3.12.
- Project governance docs: `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, and this `CHANGELOG.md`.

### Changed
- `setup.py` trimmed to the imperative packaging layout; all metadata now lives
  in `pyproject.toml`.
- `MANIFEST.in` now ships `NOTICE` and `zthird_party_licenses/` in the sdist.

## [1.1.0]

- Baseline release of the mote runtime framework.
