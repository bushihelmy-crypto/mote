#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for SkillListingContextSource (the steady, per-turn Skills index).

The source delivers the volatile ``## Available Skills`` index in the per-turn
``<system-reminder>`` (the static loading guide stays in the system prompt). The
first turn emits the whole index; later turns emit only newly-added skills
(tracked by ``_sent_names``). It is duck-typed over a single ``get_injector``
callable so it never imports the skill manager or the Role.
"""

from __future__ import annotations

import asyncio

from mote.contracts.events.conversation import PostCompactEvent
from mote.contracts.ports.conversation.turn_context import EphemeralContextSource
from mote.runtime.context.turn import SkillListingContextSource


def run(coro):
    return asyncio.run(coro)


class _FakeSkill:
    def __init__(self, name, *, is_conditional=False, disable_model_invocation=False):
        self.name = name
        self.is_conditional = is_conditional
        self.disable_model_invocation = disable_model_invocation


class _FakeInjector:
    """Mimics SkillInjector's build_index / _index_skills over a name list."""

    def __init__(self, skills):
        self._skills = list(skills)

    def _index_skills(self, only_names=None):
        out = [s for s in self._skills if not s.is_conditional and not s.disable_model_invocation]
        if only_names is not None:
            out = [s for s in out if s.name in only_names]
        return out

    def build_index(self, max_tokens=2000, only_names=None):
        skills = self._index_skills(only_names)
        if not skills:
            return ""
        return "## Available Skills\n" + "\n".join(f"- {s.name}" for s in skills)


def _source(injector):
    return SkillListingContextSource(
        get_injector=lambda: injector,
        is_enabled=lambda: True,
    )


class TestProtocol:
    def test_is_ephemeral_context_source(self):
        assert isinstance(_source(_FakeInjector([])), EphemeralContextSource)

    def test_uses_direct_rebuild_projection(self):
        source = _source(_FakeInjector([]))
        assert callable(getattr(source, "on_model_context_rebuilt", None))
        assert not getattr(source, "telemetry_observer", False)

    def test_save_to_context_true(self):
        # It is persisted to history once per turn (not request-only ephemeral).
        assert _source(_FakeInjector([])).save_to_context is True


class TestSilent:
    def test_none_injector_silent(self):
        src = SkillListingContextSource(
            get_injector=lambda: None,
            is_enabled=lambda: True,
        )
        assert run(src.render()) is None

    def test_no_indexable_skills_silent(self):
        # Only conditional / human-only skills → nothing for the steady index.
        inj = _FakeInjector(
            [
                _FakeSkill("cond", is_conditional=True),
                _FakeSkill("hidden", disable_model_invocation=True),
            ]
        )
        assert run(_source(inj).render()) is None


class TestEnabledGate:
    def test_switch_off_never_renders(self):
        # Master switch off (``config.context.skills.enabled`` False) → the index
        # never renders, even with a healthy injector holding indexable skills.
        inj = _FakeInjector([_FakeSkill("alpha")])
        src = SkillListingContextSource(get_injector=lambda: inj, is_enabled=lambda: False)
        assert run(src.render()) is None
        # No frontier bookkeeping happens while gated off.
        assert src._sent_names == set()

    def test_switch_on_renders_as_usual(self):
        inj = _FakeInjector([_FakeSkill("alpha")])
        src = SkillListingContextSource(get_injector=lambda: inj, is_enabled=lambda: True)
        out = run(src.render())
        assert out is not None
        assert "alpha" in out

    def test_enabled_policy_is_explicit(self):
        inj = _FakeInjector([_FakeSkill("alpha")])
        src = SkillListingContextSource(
            get_injector=lambda: inj,
            is_enabled=lambda: True,
        )
        assert run(src.render()) is not None


class TestFirstTurn:
    def test_emits_full_index(self):
        inj = _FakeInjector([_FakeSkill("alpha"), _FakeSkill("beta")])
        out = run(_source(inj).render())
        assert out is not None
        assert "## Available Skills" in out
        assert "alpha" in out and "beta" in out

    def test_marks_sent_names(self):
        inj = _FakeInjector([_FakeSkill("alpha")])
        src = _source(inj)
        run(src.render())
        assert src._sent_names == {"alpha"}


class TestIncremental:
    def test_second_turn_no_change_silent(self):
        inj = _FakeInjector([_FakeSkill("alpha")])
        src = _source(inj)
        run(src.render())  # first turn: full
        assert run(src.render()) is None  # nothing new → silent

    def test_new_skill_emitted_incrementally(self):
        inj = _FakeInjector([_FakeSkill("alpha")])
        src = _source(inj)
        run(src.render())  # sends alpha
        inj._skills.append(_FakeSkill("beta"))  # hot-reload adds beta
        out = run(src.render())
        assert out is not None
        assert "beta" in out
        assert "alpha" not in out  # only the delta
        assert "New Skills available" in out
        assert src._sent_names == {"alpha", "beta"}

    def test_removed_skill_not_reannounced(self):
        inj = _FakeInjector([_FakeSkill("alpha"), _FakeSkill("beta")])
        src = _source(inj)
        run(src.render())  # sends alpha, beta
        inj._skills = [_FakeSkill("alpha")]  # beta unloaded
        # Nothing new to add → silent (removals are not re-announced).
        assert run(src.render()) is None


class TestPostCompactReset:
    def test_post_compact_resets_frontier_and_resends_full(self):
        inj = _FakeInjector([_FakeSkill("alpha"), _FakeSkill("beta")])
        src = _source(inj)
        run(src.render())  # first turn: full index
        assert run(src.render()) is None  # steady: nothing new

        run(src.on_model_context_rebuilt(PostCompactEvent()))
        assert src._sent_names == set()

        out = run(src.render())  # next turn re-sends the WHOLE index
        assert out is not None
        assert "## Available Skills" in out  # full render, not the delta header
        assert "alpha" in out and "beta" in out
        assert "New Skills available" not in out

    def test_handle_ignores_other_events(self):
        inj = _FakeInjector([_FakeSkill("alpha")])
        src = _source(inj)
        run(src.render())
        run(src.on_model_context_rebuilt(object()))
        assert src._sent_names == {"alpha"}
