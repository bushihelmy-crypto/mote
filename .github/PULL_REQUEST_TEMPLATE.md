<!-- Thanks for contributing to mote! Please fill in the sections below. -->

## Summary

<!-- What does this PR change and why? Link related issues with "Closes #123". -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor (no behavior change)
- [ ] Documentation
- [ ] Build / CI / tooling

## Layering

<!-- mote enforces a strict dependency direction (see AGENTS.md):
     common ◀ context/executor/router/session ◀ parser/think/loop ◀ roles ◀ environment ◀ cli -->

- [ ] This change respects the layering rules (no upward imports; cross-layer via `common/interface/` Protocols)

## Checklist

- [ ] Tests added/updated under `ztest/<subsystem>/`
- [ ] `pre-commit run --all-files` passes (isort / black / flake8 / pyright)
- [ ] `pytest` passes locally
- [ ] Human-facing strings go through i18n (`common/i18n/`); model-facing text stays plain English
- [ ] Docs updated if behavior/public API changed
