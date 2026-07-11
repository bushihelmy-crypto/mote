#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the built-in turn_context sources (git / token / lsp).

Each source is duck-typed and self-suppressing (returns None when there is
nothing to report), so the bus can wire them unconditionally.
"""
from __future__ import annotations

import asyncio

from mote.common.events import PostCompactEvent, SessionStartEvent, TurnEndEvent
from mote.common.interface import EphemeralContextSource, ObservationSubscriber
from mote.common.schema import FoldState
from mote.context.turn_context import (
    CompactionNoticeContextSource,
    FoldPressureContextSource,
    GitContextSource,
    TokenPressureContextSource,
)


def run(coro):
    return asyncio.run(coro)


class TestProtocolConformance:
    def test_sources_are_ephemeral_context_sources(self):
        assert isinstance(GitContextSource(), EphemeralContextSource)
        assert isinstance(TokenPressureContextSource(None), EphemeralContextSource)
        assert isinstance(FoldPressureContextSource(None), EphemeralContextSource)
        assert isinstance(CompactionNoticeContextSource(), EphemeralContextSource)

    def test_compaction_notice_is_also_event_subscriber(self):
        # Dual-role: it consumes the bus event AND renders the turn-context block.
        assert isinstance(CompactionNoticeContextSource(), ObservationSubscriber)

    def test_git_source_is_also_event_subscriber(self):
        # Dual-role: it freezes the snapshot off bus events AND renders it.
        assert isinstance(GitContextSource(), ObservationSubscriber)


# --------------------------------------------------------------------------
# Git — point-in-time snapshot, armed at session-start / post-compaction.
# --------------------------------------------------------------------------
def _stub_git(monkeypatch, *, state, section=" - Git branch: main"):
    """Stub collect_git_state -> *state* and render_git_section -> *section*."""
    import mote.context.turn_context.sources.git as gitmod

    async def fake_collect(cwd):
        return state

    monkeypatch.setattr(gitmod, "collect_git_state", fake_collect)
    if section is not None:
        monkeypatch.setattr(gitmod, "render_git_section", lambda s: section)


def _session_start(cwd="/x"):
    return SessionStartEvent(working_dir=cwd)


class TestGitContextSource:
    def test_priority_and_name(self):
        s = GitContextSource()
        assert s.name == "git" and s.priority == 10

    def test_silent_until_armed(self, monkeypatch):
        # No capture event yet -> nothing to render (not a per-turn live feed).
        _stub_git(monkeypatch, state=object())
        assert run(GitContextSource().render(cwd="/x")) is None

    def test_session_start_freezes_and_renders_once(self, monkeypatch):
        _stub_git(monkeypatch, state=object())
        src = GitContextSource()
        run(src.handle(_session_start("/repo")))
        out = run(src.render(cwd="/x"))
        assert out is not None and out.startswith(" - Git branch: main")
        # Snapshot footer makes the point-in-time contract explicit.
        assert "snapshot" in out.lower()
        # Disarms — a second cycle with no new event is silent.
        assert run(src.render(cwd="/x")) is None

    def test_post_compact_recaptures_via_cwd_provider(self, monkeypatch):
        _stub_git(monkeypatch, state=object())
        src = GitContextSource(get_cwd=lambda: "/live")
        run(src.handle(PostCompactEvent(summary="x")))
        assert run(src.render(cwd="/x")) is not None
        assert run(src.render(cwd="/x")) is None  # disarmed

    def test_none_snapshot_is_faithful_and_disarms(self, monkeypatch):
        # off-repo at capture time -> None is a legitimate snapshot; render nothing
        # but still disarm (no retry, no live tracking).
        _stub_git(monkeypatch, state=None)
        src = GitContextSource()
        run(src.handle(_session_start("/x")))
        assert run(src.render(cwd="/x")) is None
        # Not retried on the next cycle either.
        assert run(src.render(cwd="/x")) is None

    def test_empty_section_collapses_to_none(self, monkeypatch):
        _stub_git(monkeypatch, state=object(), section="")
        src = GitContextSource()
        run(src.handle(_session_start("/x")))
        assert run(src.render(cwd="/x")) is None

    def test_recapture_between_renders_shows_latest(self, monkeypatch):
        import mote.context.turn_context.sources.git as gitmod

        sections = iter([" - Git branch: main", " - Git branch: feature"])

        async def fake_collect(cwd):
            return object()

        monkeypatch.setattr(gitmod, "collect_git_state", fake_collect)
        monkeypatch.setattr(gitmod, "render_git_section", lambda s: next(sections))

        src = GitContextSource(get_cwd=lambda: "/repo")
        run(src.handle(_session_start("/repo")))
        first = run(src.render(cwd="/x"))
        assert first.startswith(" - Git branch: main")
        # A compaction re-freezes the snapshot -> next render shows the new one.
        run(src.handle(PostCompactEvent(summary="x")))
        second = run(src.render(cwd="/x"))
        assert second.startswith(" - Git branch: feature")

    def test_no_cwd_provider_post_compact_is_silent(self, monkeypatch):
        # PostCompact with no get_cwd provider -> capture(None) -> nothing.
        _stub_git(monkeypatch, state=object())
        src = GitContextSource()  # no get_cwd
        run(src.handle(PostCompactEvent(summary="x")))
        assert run(src.render(cwd="/x")) is None

    def test_capture_failure_is_swallowed(self, monkeypatch):
        import mote.context.turn_context.sources.git as gitmod

        async def boom(cwd):
            raise RuntimeError("git blew up")

        monkeypatch.setattr(gitmod, "collect_git_state", boom)
        src = GitContextSource()
        run(src.handle(_session_start("/repo")))  # must not raise
        assert run(src.render(cwd="/x")) is None


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
# Fold pressure — count-based sibling of token pressure. Uses the REAL
# ``FoldState`` so its ``near_fold`` (ceil(trigger*0.8) window) is exercised too.
# --------------------------------------------------------------------------
class _FakeFoldProvider:
    def __init__(self, state):
        self._state = state

    def fold_state(self):
        return self._state


class _SeqFoldProvider:
    """Yields a scripted sequence of FoldStates, one per ``fold_state()`` call.

    Drives the edge-trigger tests: successive renders see successive counts, so
    the latch's rising-edge behaviour (fire on entry, silent while parked in the
    window, re-arm after leaving) can be exercised turn by turn.
    """

    def __init__(self, states):
        self._states = list(states)
        self._i = 0

    def fold_state(self):
        s = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return s


def _fold(count, *, enabled=True, trigger=10, keep_recent=5):
    return FoldState(enabled=enabled, active_count=count, trigger=trigger, keep_recent=keep_recent)


class TestFoldPressureContextSource:
    def test_none_provider_returns_none(self):
        assert run(FoldPressureContextSource(None).render()) is None

    def test_none_state_returns_none(self):
        assert run(FoldPressureContextSource(_FakeFoldProvider(None)).render()) is None

    def test_below_warning_window_returns_none(self):
        # trigger 10 -> warn at ceil(8)=8; 7 is still silent.
        src = FoldPressureContextSource(_FakeFoldProvider(_fold(7)))
        assert run(src.render()) is None

    def test_at_warning_window_emits_block_with_keep_recent(self):
        src = FoldPressureContextSource(_FakeFoldProvider(_fold(8)))
        out = run(src.render())
        assert out is not None
        assert "clearing imminent" in out.lower()
        assert "5 most recent" in out  # keep_recent surfaced

    def test_past_trigger_returns_none(self):
        # Once past the trigger the fold has (or is about to have) already run —
        # the pre-warning window is closed, so stay silent rather than nag.
        src = FoldPressureContextSource(_FakeFoldProvider(_fold(11)))
        assert run(src.render()) is None

    def test_disabled_returns_none(self):
        src = FoldPressureContextSource(_FakeFoldProvider(_fold(10, enabled=False)))
        assert run(src.render()) is None

    def test_lazy_getter_provider_is_resolved(self):
        src = FoldPressureContextSource(lambda: _FakeFoldProvider(_fold(9)))
        assert run(src.render()) is not None

    def test_edge_trigger_fires_once_while_parked_in_window(self):
        # Counts climb 7->8->9->10: silent below the window, one warning on the
        # rising edge (7->8), then silent while parked inside it (9, 10) — no
        # per-turn nagging.
        src = FoldPressureContextSource(_SeqFoldProvider([_fold(7), _fold(8), _fold(9), _fold(10)]))
        assert run(src.render()) is None  # 7: below window
        assert run(src.render()) is not None  # 8: rising edge -> fire once
        assert run(src.render()) is None  # 9: still in window -> silent
        assert run(src.render()) is None  # 10: still in window -> silent

    def test_edge_trigger_rearms_after_leaving_window(self):
        # A fold drops the count back below the window; the next approach must
        # re-fire (one nudge per approach, not one per session).
        src = FoldPressureContextSource(_SeqFoldProvider([_fold(8), _fold(3), _fold(8)]))
        assert run(src.render()) is not None  # rising edge -> fire
        assert run(src.render()) is None  # folded back to 3 -> re-arm, silent
        assert run(src.render()) is not None  # approaches again -> fire again

    def test_edge_trigger_fires_when_jumping_straight_into_window(self):
        # Below window then straight to the trigger boundary (no intermediate
        # step): still a rising edge, so it fires.
        src = FoldPressureContextSource(_SeqFoldProvider([_fold(7), _fold(10)]))
        assert run(src.render()) is None
        assert run(src.render()) is not None

    def test_priority_and_name(self):
        s = FoldPressureContextSource(None)
        assert s.name == "fold" and s.priority == 22


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
