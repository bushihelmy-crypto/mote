#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the built-in turn_context sources (git / token / lsp).

Each source is duck-typed and self-suppressing (returns None when there is
nothing to report), so the bus can wire them unconditionally.
"""
from __future__ import annotations

import asyncio

from metagpt.common.events import PostCompactEvent, TurnEndEvent
from metagpt.common.interface import EphemeralContextSource, EventSubscriber
from metagpt.context.turn_context import (
    CompactionNoticeContextSource,
    GitContextSource,
    TokenPressureContextSource,
)


def run(coro):
    return asyncio.run(coro)


class TestProtocolConformance:
    def test_sources_are_ephemeral_context_sources(self):
        assert isinstance(GitContextSource(), EphemeralContextSource)
        assert isinstance(TokenPressureContextSource(None), EphemeralContextSource)
        assert isinstance(CompactionNoticeContextSource(), EphemeralContextSource)

    def test_compaction_notice_is_also_event_subscriber(self):
        # Dual-role: it consumes the bus event AND renders the turn-context block.
        assert isinstance(CompactionNoticeContextSource(), EventSubscriber)


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


# Note: the LSP feed is the dual-role `DiagnosticsBuffer` (it is both the bus
# subscriber and the EphemeralContextSource), so its render path is covered in
# ztest/lsp/test_service_integration.py rather than here.


# --------------------------------------------------------------------------
# Compaction notice
# --------------------------------------------------------------------------
class TestCompactionNoticeContextSource:
    def test_priority_and_name(self):
        s = CompactionNoticeContextSource()
        assert s.name == "compaction" and s.priority == 25

    def test_silent_before_any_compaction(self):
        assert run(CompactionNoticeContextSource().render()) is None

    def test_renders_once_after_post_compact_then_disarms(self):
        s = CompactionNoticeContextSource()
        run(s.handle(PostCompactEvent(trigger="auto", summary="prev work")))
        out = run(s.render())
        assert out is not None
        assert "History compacted" in out
        # One-shot: the next cycle is silent again.
        assert run(s.render()) is None

    def test_multiple_compactions_collapse_to_one_notice(self):
        s = CompactionNoticeContextSource()
        run(s.handle(PostCompactEvent()))
        run(s.handle(PostCompactEvent()))
        assert run(s.render()) is not None
        assert run(s.render()) is None

    def test_ignores_unrelated_events(self):
        s = CompactionNoticeContextSource()
        run(s.handle(TurnEndEvent()))
        assert run(s.render()) is None

    def test_summary_not_echoed_in_notice(self):
        # The summary already lives in history; the notice only flags the event.
        s = CompactionNoticeContextSource()
        run(s.handle(PostCompactEvent(summary="SECRET-SUMMARY-TEXT")))
        out = run(s.render())
        assert "SECRET-SUMMARY-TEXT" not in out
