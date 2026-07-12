#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for SkillActivationContextSource (path-gated conditional skills).

The source surfaces a conditional skill's index row into the per-turn
``<system-reminder>`` only when a recently-touched file matches one of its
``paths`` / ``globs``. It is duck-typed over two callables (``get_pool`` /
``get_touched_files``) so it never imports the pool or the Role.
"""
from __future__ import annotations

import asyncio

from metagpt.common.interface import EphemeralContextSource
from metagpt.context.skills.skill_definition import SkillDefinition
from metagpt.context.turn_context import SkillActivationContextSource


def run(coro):
    return asyncio.run(coro)


def _skill(name, *, paths=None, globs=None, description="A skill", **kwargs):
    from pathlib import Path

    return SkillDefinition(
        name=name,
        description=description,
        instructions="body",
        source_path=Path(),
        paths=paths or [],
        globs=globs or [],
        **kwargs,
    )


class _FakePool:
    def __init__(self, skills):
        self._skills = skills

    def get_all(self):
        return list(self._skills)


def _source(skills, touched):
    return SkillActivationContextSource(
        get_pool=lambda: _FakePool(skills),
        get_touched_files=lambda: list(touched),
    )


class TestProtocol:
    def test_is_ephemeral_context_source(self):
        src = _source([], [])
        assert isinstance(src, EphemeralContextSource)


class TestSilent:
    def test_none_pool_silent(self):
        src = SkillActivationContextSource(
            get_pool=lambda: None, get_touched_files=lambda: ["/a.py"]
        )
        assert run(src.render(cwd="/")) is None

    def test_no_conditional_skills_silent(self):
        plain = _skill("plain")  # no paths/globs → not conditional
        src = _source([plain], ["/proj/a.py"])
        assert run(src.render(cwd="/proj")) is None

    def test_no_touched_files_silent(self):
        cond = _skill("cond", globs=["*.py"])
        src = _source([cond], [])
        assert run(src.render(cwd="/proj")) is None

    def test_no_match_silent(self):
        cond = _skill("cond", globs=["*.md"])
        src = _source([cond], ["/proj/a.py"])
        assert run(src.render(cwd="/proj")) is None

    def test_human_only_excluded(self):
        cond = _skill("cond", globs=["*.py"], disable_model_invocation=True)
        src = _source([cond], ["/proj/a.py"])
        assert run(src.render(cwd="/proj")) is None


class TestMatch:
    def test_basename_glob_match(self):
        cond = _skill("py-helper", globs=["*.py"], description="Edit Python")
        src = _source([cond], ["/proj/src/module.py"])
        out = run(src.render(cwd="/proj"))
        assert out is not None
        assert "py-helper" in out
        assert "# Relevant Skills" in out

    def test_relpath_glob_match(self):
        cond = _skill("api", paths=["src/api/*.py"])
        src = _source([cond], ["/proj/src/api/handler.py"])
        out = run(src.render(cwd="/proj"))
        assert out is not None and "api" in out

    def test_paths_and_globs_both_considered(self):
        cond = _skill("docs", paths=["docs/*.md"])
        src = _source([cond], ["/proj/docs/readme.md"])
        out = run(src.render(cwd="/proj"))
        assert out is not None and "docs" in out

    def test_argument_hint_in_row(self):
        cond = _skill("api", globs=["*.py"], argument_hint="<endpoint>")
        src = _source([cond], ["/proj/a.py"])
        out = run(src.render(cwd="/proj"))
        assert "[args: <endpoint>]" in out

    def test_when_to_use_merged(self):
        cond = _skill("api", globs=["*.py"], when_to_use="editing endpoints")
        src = _source([cond], ["/proj/a.py"])
        out = run(src.render(cwd="/proj"))
        assert "use when: editing endpoints" in out

    def test_only_matching_skill_listed(self):
        a = _skill("py-skill", globs=["*.py"])
        b = _skill("md-skill", globs=["*.md"])
        src = _source([a, b], ["/proj/a.py"])
        out = run(src.render(cwd="/proj"))
        assert "py-skill" in out
        assert "md-skill" not in out
