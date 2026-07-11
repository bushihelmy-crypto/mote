#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures + helpers for the skills test suite.

The skills subsystem is filesystem-driven: ``SkillPool`` scans a *builtin*
directory of ``SKILL.md`` files and ``SkillInjector`` builds prompt content
(an in-memory index) from the loaded pool.  The
real builtin dir ships empty, so every test fabricates its own skill tree
under ``tmp_path`` via ``write_skill`` and points the relevant module
constant at it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest
import yaml
from mote.context.skills.skill_definition import SkillDefinition
from mote.context.skills.skill_pool import SkillPool


def write_skill(
    builtin_dir: Path,
    name: str,
    *,
    description: str = "A test skill that does a thing.",
    globs: Optional[list[str]] = None,
    instructions: str = "Step 1. Do the thing.\nStep 2. Profit.",
    extra_meta: Optional[dict] = None,
    dir_name: Optional[str] = None,
    raw: Optional[str] = None,
) -> Path:
    """Write a ``<builtin>/<dir_name>/SKILL.md`` file and return its path.

    ``dir_name`` lets the directory differ from the metadata ``name`` (used to
    exercise the dir-name fallback / underscore-skip behaviour).  ``raw`` writes
    arbitrary file content verbatim (bypassing the frontmatter builder).
    """
    skill_dir = builtin_dir / (dir_name or name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"

    if raw is not None:
        skill_md.write_text(raw, encoding="utf-8")
        return skill_md

    meta: dict = {"name": name, "description": description}
    if globs is not None:
        meta["globs"] = globs
    if extra_meta:
        meta.update(extra_meta)

    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    skill_md.write_text(f"---\n{front}---\n\n{instructions}\n", encoding="utf-8")
    return skill_md


def make_skill_def(
    *,
    name: str = "demo-skill",
    description: str = "A demo skill.",
    instructions: str = "Do it.",
    source_path: Optional[Path] = None,
    **kwargs,
) -> SkillDefinition:
    """Build a SkillDefinition directly (no filesystem)."""
    return SkillDefinition(
        name=name,
        description=description,
        instructions=instructions,
        source_path=source_path or Path(),
        **kwargs,
    )


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    """An empty builtin directory tests populate with write_skill()."""
    d = tmp_path / "builtin"
    d.mkdir()
    return d


@pytest.fixture
def pool_factory(builtin_dir):
    """Return a callable that builds a SkillPool bound to the temp builtin dir."""

    def _factory(names: list[str]) -> SkillPool:
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(names)
        return pool

    return _factory
