#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the built-in turn_context sources (git / token / lsp).

Each source is duck-typed and self-suppressing (returns None when there is
nothing to report), so the bus can wire them unconditionally.
"""
from __future__ import annotations

import asyncio

from metagpt.common.interface import EphemeralContextSource
from metagpt.context.turn_context import (
    GitContextSource,
    LspContextSource,
    TokenPressureContextSource,
)


def run(coro):
    return asyncio.run(coro)


class TestProtocolConformance:
    def test_sources_are_ephemeral_context_sources(self):
        assert isinstance(GitContextSource(), EphemeralContextSource)
        assert isinstance(LspContextSource(None), EphemeralContextSource)
        assert isinstance(TokenPressureContextSource(None), EphemeralContextSource)


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------
class TestGitContextSource:
    def test_none_state_returns_none(self, monkeypatch):
        import metagpt.context.turn_context.sources.git as gitmod

        async def fake_collect(cwd):
            return None

        monkeypatch.setattr(gitmod, "collect_git_state", fake_collect)
        assert run(GitContextSource().render(cwd="/x")) is None

    def test_renders_section_when_state_present(self, monkeypatch):
        import metagpt.context.turn_context.sources.git as gitmod

        async def fake_collect(cwd):
            return object()  # truthy sentinel

        monkeypatch.setattr(gitmod, "collect_git_state", fake_collect)
        monkeypatch.setattr(gitmod, "render_git_section", lambda s: " - Git branch: main")
        assert run(GitContextSource().render(cwd="/x")) == " - Git branch: main"

    def test_empty_render_collapses_to_none(self, monkeypatch):
        import metagpt.context.turn_context.sources.git as gitmod

        async def fake_collect(cwd):
            return object()

        monkeypatch.setattr(gitmod, "collect_git_state", fake_collect)
        monkeypatch.setattr(gitmod, "render_git_section", lambda s: "")
        assert run(GitContextSource().render(cwd="/x")) is None

    def test_priority_and_name(self):
        s = GitContextSource()
        assert s.name == "git" and s.priority == 10


# --------------------------------------------------------------------------
# Token pressure
# --------------------------------------------------------------------------
class _FakeTokenState:
    def __init__(self, above_warning, percent_left=0):
        self.above_warning = above_warning
        self.percent_left = percent_left


class _FakeProvider:
    def __init__(self, state):
        self._state = state

    def token_state(self):
        return self._state


class TestTokenPressureContextSource:
    def test_none_provider_returns_none(self):
        assert run(TokenPressureContextSource(None).render()) is None

    def test_below_warning_returns_none(self):
        src = TokenPressureContextSource(_FakeProvider(_FakeTokenState(False, 80)))
        assert run(src.render()) is None

    def test_none_state_returns_none(self):
        src = TokenPressureContextSource(_FakeProvider(None))
        assert run(src.render()) is None

    def test_above_warning_emits_block_with_percent(self):
        src = TokenPressureContextSource(_FakeProvider(_FakeTokenState(True, 12)))
        out = run(src.render())
        assert out is not None
        assert "Context budget" in out
        assert "12%" in out

    def test_priority_and_name(self):
        s = TokenPressureContextSource(None)
        assert s.name == "token" and s.priority == 20


# --------------------------------------------------------------------------
# LSP
# --------------------------------------------------------------------------
class _FakeLsp:
    def __init__(self, block):
        self._block = block

    def drain_diagnostics(self):
        return self._block


class TestLspContextSource:
    def test_none_service_returns_none(self):
        assert run(LspContextSource(None).render()) is None

    def test_empty_drain_returns_none(self):
        assert run(LspContextSource(_FakeLsp("")).render()) is None

    def test_block_returned(self):
        out = run(LspContextSource(_FakeLsp("<lsp_diagnostics>...")).render())
        assert out == "<lsp_diagnostics>..."

    def test_priority_and_name(self):
        s = LspContextSource(None)
        assert s.name == "lsp" and s.priority == 40
