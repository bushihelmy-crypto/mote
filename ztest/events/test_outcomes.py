#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the typed control-plane outcomes (``common/events/outcomes.py``).

Each control event has its own small outcome DTO. Every one satisfies the
``ControlOutcome`` protocol the bus drives generically — ``is_blocking`` (a
deny/stop short-circuits the bucket), ``merge`` (fold two same-event outcomes
with the hook layer's precedence: deny>ask>allow, accumulated context, last
rewrite wins, sticky stop), and ``rebind`` (thread a rewrite forward). These
tests pin that per-event contract so a new outcome can be checked against the
same shape. ``rebind`` delegates to the event's generic ``rewrite(field, after,
*, by)`` (the :class:`Rewritable` primitive), which records the mutation with its
before-image and the rewriting subscriber's ``by`` attribution as provenance.
"""
from __future__ import annotations

from dataclasses import dataclass

from metagpt.common.events import (
    CompactOutcome,
    PromptOutcome,
    Rewrite,
    Rewritable,
    SpawnOutcome,
    ToolCallOutcome,
    ToolResultOutcome,
    TurnOutcome,
)
from metagpt.common.interface.event_subscriber import ControlOutcome


# Minimal Rewritable event stubs: inherit the nominal :class:`Rewritable` mixin
# (so ``rebind``'s ``isinstance(event, Rewritable)`` guard passes) and get its
# generic ``rewrite`` — which records the before-image and ``by`` attribution on
# ``rewrites``, exactly like the real events.
@dataclass
class _ArgsEvent(Rewritable):
    tool_input: dict = None


@dataclass
class _RespEvent(Rewritable):
    tool_response: str = ""


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_all_outcomes_satisfy_control_outcome_protocol():
    for out in (
        ToolCallOutcome(),
        ToolResultOutcome(),
        PromptOutcome(),
        CompactOutcome(),
        SpawnOutcome(),
        TurnOutcome(),
    ):
        assert isinstance(out, ControlOutcome)


# ---------------------------------------------------------------------------
# ToolCallOutcome
# ---------------------------------------------------------------------------


def test_tool_call_is_blocking_on_deny_or_stop():
    assert ToolCallOutcome(behavior="deny").is_blocking
    assert ToolCallOutcome(stop=True).is_blocking
    assert not ToolCallOutcome(behavior="allow").is_blocking
    assert not ToolCallOutcome().is_blocking


def test_tool_call_merge_folds_deny_over_allow():
    merged = ToolCallOutcome(behavior="allow").merge(ToolCallOutcome(behavior="deny"))
    assert merged.behavior == "deny"
    # order-independent (deny wins either way)
    merged2 = ToolCallOutcome(behavior="deny").merge(ToolCallOutcome(behavior="allow"))
    assert merged2.behavior == "deny"


def test_tool_call_merge_ask_beats_allow():
    merged = ToolCallOutcome(behavior="allow").merge(ToolCallOutcome(behavior="ask"))
    assert merged.behavior == "ask"


def test_tool_call_merge_last_updated_args_wins():
    merged = ToolCallOutcome(updated_args={"cmd": "a"}).merge(ToolCallOutcome(updated_args={"cmd": "b"}))
    assert merged.updated_args == {"cmd": "b"}
    # a None on the right keeps the left value.
    merged2 = ToolCallOutcome(updated_args={"cmd": "a"}).merge(ToolCallOutcome())
    assert merged2.updated_args == {"cmd": "a"}


def test_tool_call_merge_sticky_stop():
    merged = ToolCallOutcome(stop=True).merge(ToolCallOutcome(stop=False))
    assert merged.stop is True


def test_tool_call_rebind_threads_updated_args():
    out = ToolCallOutcome(updated_args={"cmd": "safe"})
    threaded = out.rebind(_ArgsEvent(tool_input={"cmd": "danger"}), by="hook")
    assert threaded.tool_input == {"cmd": "safe"}


def test_tool_call_rebind_records_provenance():
    out = ToolCallOutcome(updated_args={"cmd": "safe"})
    threaded = out.rebind(_ArgsEvent(tool_input={"cmd": "danger"}), by="hook")
    assert threaded.rewrites == (
        Rewrite(field="tool_input", before={"cmd": "danger"}, after={"cmd": "safe"}, by="hook"),
    )


def test_tool_call_rebind_no_args_returns_event_unchanged():
    ev = _ArgsEvent(tool_input={"cmd": "x"})
    assert ToolCallOutcome().rebind(ev, by="hook") is ev


# ---------------------------------------------------------------------------
# ToolResultOutcome
# ---------------------------------------------------------------------------


def test_tool_result_is_blocking_on_blocked():
    assert ToolResultOutcome(blocked=True).is_blocking
    assert not ToolResultOutcome().is_blocking


def test_tool_result_merge_accumulates_context_and_last_response():
    merged = ToolResultOutcome(updated_response="a", additional_context=["x"]).merge(
        ToolResultOutcome(updated_response="b", additional_context=["y"])
    )
    assert merged.updated_response == "b"
    assert merged.additional_context == ["x", "y"]


def test_tool_result_merge_sticky_blocked():
    merged = ToolResultOutcome(blocked=True).merge(ToolResultOutcome(blocked=False))
    assert merged.blocked is True


def test_tool_result_rebind_threads_updated_response():
    out = ToolResultOutcome(updated_response="[redacted]")
    threaded = out.rebind(_RespEvent(tool_response="secret"), by="hook")
    assert threaded.tool_response == "[redacted]"


def test_tool_result_rebind_records_provenance():
    out = ToolResultOutcome(updated_response="[redacted]")
    threaded = out.rebind(_RespEvent(tool_response="secret"), by="hook")
    assert threaded.rewrites == (
        Rewrite(field="tool_response", before="secret", after="[redacted]", by="hook"),
    )


# ---------------------------------------------------------------------------
# PromptOutcome / CompactOutcome / SpawnOutcome / TurnOutcome
# ---------------------------------------------------------------------------


def test_prompt_outcome_stop_and_context():
    assert PromptOutcome(stop=True).is_blocking
    merged = PromptOutcome(additional_context=["a"]).merge(PromptOutcome(additional_context=["b"], stop=True))
    assert merged.additional_context == ["a", "b"]
    assert merged.is_blocking


def test_compact_outcome_cancel_and_context():
    assert CompactOutcome(cancel=True).is_blocking
    merged = CompactOutcome(additional_context=["a"]).merge(CompactOutcome(cancel=True, additional_context=["b"]))
    assert merged.cancel is True
    assert merged.additional_context == ["a", "b"]


def test_spawn_outcome_denied_and_reason_last_wins():
    assert SpawnOutcome(denied=True).is_blocking
    assert not SpawnOutcome().is_blocking
    merged = SpawnOutcome(reason="first").merge(SpawnOutcome(denied=True, reason="second"))
    assert merged.denied is True
    assert merged.reason == "second"


def test_turn_outcome_block_and_context():
    assert TurnOutcome(block=True).is_blocking
    merged = TurnOutcome(additional_context=["a"], system_message="m1").merge(
        TurnOutcome(block=True, additional_context=["b"], system_message="m2")
    )
    assert merged.block is True
    assert merged.additional_context == ["a", "b"]
    assert merged.system_message == "m2"


def test_non_rewriting_outcomes_rebind_returns_event_unchanged():
    sentinel = object()
    for out in (PromptOutcome(), CompactOutcome(), SpawnOutcome(), TurnOutcome()):
        assert out.rebind(sentinel, by="anyone") is sentinel


# ---------------------------------------------------------------------------
# Nominal contract enforcement
# ---------------------------------------------------------------------------


def test_outcome_missing_is_blocking_and_merge_cannot_instantiate():
    """An outcome subclass that forgets the abstract ``is_blocking``/``merge``
    is abstract — caught at construction, not at the bus fold site."""
    import pytest

    class Incomplete(ControlOutcome):
        pass

    with pytest.raises(TypeError):
        Incomplete()


def test_rebind_on_non_rewritable_event_raises_when_updated_args_set():
    """When an outcome carries a rewrite but the target event is not
    :class:`Rewritable`, ``rebind`` fails loud rather than silently dropping the
    rewrite (the old ``hasattr`` sniff)."""
    import pytest

    class NotRewritable:
        tool_input = {"cmd": "x"}

    out = ToolCallOutcome(updated_args={"cmd": "safe"})
    with pytest.raises(TypeError, match="Rewritable"):
        out.rebind(NotRewritable(), by="hook")


def test_rebind_on_non_rewritable_event_inert_when_no_rewrite():
    """No rewrite carried → ``rebind`` returns the event untouched even for a
    non-Rewritable event (the guard only fires when a rewrite is present)."""

    class NotRewritable:
        tool_input = {"cmd": "x"}

    ev = NotRewritable()
    assert ToolCallOutcome().rebind(ev, by="hook") is ev
