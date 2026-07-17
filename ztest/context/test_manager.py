#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.context.manager.ContextManager`` — the facade.

Two responsibilities:

1. Message store (the slice of the old ``Memory`` the loop depends on):
   ``get`` / ``add`` / ``add_batch`` / ``delete`` / ``count`` / ``clear`` /
   ``messages`` — all backed by the injected ``LLMCallContext`` so the history is
   checkpointed.
2. History orchestration: ``manage_history`` runs microcompact then autocompact,
   ``token_state`` reports the budget, and ``prepare_request`` assembles the
   per-call request (managed history + the user prompt, without storing it).

Compaction-triggering tests reuse ``force_autocompact_threshold`` and small
configs so the real gates fire on tiny inputs.
"""
from __future__ import annotations

import pytest

from mote.common.schema import ContextManagerConfig, LLMCallContext, UserMessage
from mote.context import ContextManager

from .conftest import COMPACTABLE, FakeLLM, make_pairs, text_msg

MICRO_CFG = ContextManagerConfig(
    microcompact_trigger_threshold=2,
    microcompact_keep_recent=1,
    enable_autocompact=False,
    # Tiny test bodies fold to a handful of tokens; drop the cache-write gate so
    # the count-driven fold still fires (the token gate is tested in test_fold).
    microcompact_clear_at_least=0,
)


# ---------------------------------------------------------------------------
# Message-store API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_count_and_get_all():
    cm = ContextManager(model="gpt-4")
    await cm.add(text_msg("a"))
    await cm.add(text_msg("b"))
    assert cm.count() == 2
    assert [m.content for m in cm.get()] == ["a", "b"]


def test_recovery_reducer_includes_summarize():
    """The reactive (HARD) recovery reducer now escalates fold → summarize → drop
    (summarize preserves history far better than a raw head-drop; drop is the floor)."""
    from mote.context.compaction.reducers.drop import HeadDropReducer
    from mote.context.compaction.reducers.fold import FoldReducer
    from mote.context.compaction.reducers.summarize import SummarizeReducer

    cm = ContextManager(model="gpt-4")
    reducers = cm.recovery_reducer._pipeline._reducers
    types = {type(r) for r in reducers}
    assert FoldReducer in types
    assert SummarizeReducer in types
    assert HeadDropReducer in types


def test_compactable_defaults_to_empty_set():
    """With no injected set (standalone/test use), nothing is foldable. The
    manager threads the set only into ``Transcript.from_messages`` (the single
    place the reconstructable judgment is made); production injects the
    executor-derived set."""
    cm = ContextManager(model="gpt-4")
    assert cm._compactable == frozenset()


def test_compactable_threaded_into_transcript():
    """An injected compactable set (e.g. derived from the live executor) is
    stored verbatim and threaded into ``Transcript.from_messages`` so folded
    groups get stamped ``reconstructable``."""
    custom = frozenset({"Read", "Grep"})
    cm = ContextManager(model="gpt-4", compactable=custom)
    assert cm._compactable == custom


@pytest.mark.asyncio
async def test_tools_changed_event_refreshes_compactable():
    """When the executor de-registers a tool it announces the fresh
    reconstructable set on the shared bus; the manager (an observer on that same
    bus) refreshes ``_compactable`` so compaction never keeps folding a result
    whose tool has since gone."""
    from mote.common.events import EventBus, ToolsChangedEvent

    bus = EventBus()
    cm = ContextManager(model="gpt-4", compactable=frozenset({"Read", "Write"}), bus=bus)
    # Subscribing in __init__ means the event routes here when the bus fans out.
    await bus.observe(ToolsChangedEvent(removed=["Write"], reconstructable=["Read"]))
    assert cm._compactable == frozenset({"Read"})


@pytest.mark.asyncio
async def test_non_tools_changed_event_leaves_compactable_untouched():
    """The manager also *emits* on the same bus; its own emissions (and any
    unrelated event) fall through ``handle`` without disturbing the set."""
    from mote.common.events import EventBus

    bus = EventBus()
    cm = ContextManager(model="gpt-4", compactable=frozenset({"Read"}), bus=bus)
    await cm.handle(object())
    assert cm._compactable == frozenset({"Read"})


# ---------------------------------------------------------------------------
# Swappable compression pipeline (rebuild_compression)
# ---------------------------------------------------------------------------


def test_build_compression_assembles_full_stack():
    """Construction wires every dependent piece of the compression stack."""
    cm = ContextManager(model="gpt-4")
    assert cm._erase is not None
    assert cm._fold is not None
    assert cm._summarize is not None
    assert cm._drop is not None
    assert cm._engine is not None
    assert cm._recovery_reducer is not None


def test_rebuild_replaces_every_reducer_instance():
    """A no-arg rebuild reconstructs the whole stack — fresh reducer objects, a
    fresh engine and recovery reducer — so nothing stale is left behind."""
    cm = ContextManager(model="gpt-4")
    before = (cm._erase, cm._fold, cm._summarize, cm._drop, cm._engine, cm._recovery_reducer)
    cm.rebuild_compression()
    after = (cm._erase, cm._fold, cm._summarize, cm._drop, cm._engine, cm._recovery_reducer)
    assert all(a is not b for a, b in zip(after, before))


def test_rebuild_with_config_retunes_reducers():
    """A retuned config threads into the freshly built reducers and is stored."""
    new_cfg = ContextManagerConfig(microcompact_trigger_threshold=99)
    cm = ContextManager(model="gpt-4")
    cm.rebuild_compression(config=new_cfg)
    assert cm.config is new_cfg
    assert cm._fold._cfg is new_cfg
    assert cm._summarize._cfg is new_cfg


def test_rebuild_with_llm_rebinds_rederives_model_and_refreshes_accountant():
    """Rebinding the LLM re-derives the model (``self.model`` reads ``llm.model``),
    threads it into the reducers, and refreshes the token accountant."""
    # No explicit model pin, so ``self.model`` derives from the bound llm.
    cm = ContextManager(llm=FakeLLM(model="orig"))
    old_accountant = cm._accountant
    big = FakeLLM(model="big-context-model")
    cm.rebuild_compression(llm=big)
    assert cm.model == "big-context-model"
    assert cm._summarize._llm is big
    assert cm._summarize._model == "big-context-model"
    assert cm._accountant is not old_accountant


def test_rebuild_omitting_args_leaves_inputs_untouched():
    """Sentinel-guarded: omitting an argument leaves that input exactly as-is."""
    llm = FakeLLM(model="orig")
    cfg = ContextManagerConfig()
    cm = ContextManager(llm=llm, config=cfg)
    cm.rebuild_compression()
    assert cm._llm is llm
    assert cm.config is cfg


def test_rebuild_llm_none_is_an_explicit_value():
    """``None`` is a meaningful llm (it disables summarize) — the sentinel keeps
    it distinct from 'leave unchanged', so passing it actually unbinds the llm."""
    cm = ContextManager(llm=FakeLLM(model="orig"))
    assert cm.model == "orig"
    cm.rebuild_compression(llm=None)
    assert cm._llm is None
    assert cm.model == "gpt-4"  # falls back to the generic default


@pytest.mark.asyncio
async def test_add_skips_none():
    cm = ContextManager(model="gpt-4")
    await cm.add(None)
    assert cm.count() == 0


@pytest.mark.asyncio
async def test_add_batch_skips_falsy():
    cm = ContextManager(model="gpt-4")
    await cm.add_batch([text_msg("a"), None, text_msg("b")])
    assert cm.count() == 2


@pytest.mark.asyncio
async def test_get_k_returns_tail():
    cm = ContextManager(model="gpt-4")
    await cm.add_batch([text_msg(str(i)) for i in range(5)])
    assert [m.content for m in cm.get(2)] == ["3", "4"]
    assert [m.content for m in cm.get(0)] == ["0", "1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_delete_present_and_absent():
    cm = ContextManager(model="gpt-4")
    m = text_msg("a")
    await cm.add(m)
    cm.delete(m)
    assert cm.count() == 0
    # deleting again (absent) is a safe no-op
    cm.delete(m)
    assert cm.count() == 0


@pytest.mark.asyncio
async def test_clear():
    from mote.common.events import HistoryEditedEvent

    bus = _RecordingBus()
    cm = ContextManager(model="gpt-4", bus=bus)
    await cm.add_batch([text_msg("a"), text_msg("b")])
    bus.emitted.clear()  # ignore the MessageAppended events from add_batch

    await cm.clear()

    assert cm.count() == 0
    # /clear announces the structural rebuild as an empty-history edit so every
    # history-derived signal (SR frontiers, the resource side-store) resets.
    edits = [e for e in bus.emitted if isinstance(e, HistoryEditedEvent)]
    assert len(edits) == 1
    assert edits[0].messages == []
    assert edits[0].reason == "clear"


@pytest.mark.asyncio
async def test_messages_backs_injected_context():
    ctx = LLMCallContext()
    cm = ContextManager(ctx, model="gpt-4")
    await cm.add(text_msg("a"))
    # the store mutates the shared context (so it gets checkpointed)
    assert ctx.messages is cm.messages
    assert [m.content for m in ctx.messages] == ["a"]


def test_model_property_fallback():
    assert ContextManager().model == "gpt-4"  # generic default
    assert ContextManager(model="explicit").model == "explicit"
    assert ContextManager(llm=FakeLLM(model="from-llm")).model == "from-llm"


@pytest.mark.asyncio
async def test_token_state_returns_snapshot():
    cm = ContextManager(model="gpt-4")
    await cm.add(text_msg("hello world"))
    state = cm.token_state()
    assert state.model == "gpt-4"
    assert state.token_count > 0


# ---------------------------------------------------------------------------
# fold_state — count-based sibling of token_state, drives the pre-fold warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fold_state_counts_foldable_results():
    # 3 reconstructable Read pairs against the injected compactable set.
    cm = ContextManager(
        model="gpt-4",
        config=ContextManagerConfig(microcompact_trigger_threshold=10, microcompact_keep_recent=5),
        compactable=COMPACTABLE,
    )
    await cm.add_batch(make_pairs(3, name="Read"))
    state = cm.fold_state()
    assert state.enabled is True
    assert state.active_count == 3
    assert state.trigger == 10
    assert state.keep_recent == 5
    assert state.near_fold is False  # 3 < ceil(10*0.8)=8


@pytest.mark.asyncio
async def test_fold_state_near_fold_in_warning_window():
    cm = ContextManager(
        model="gpt-4",
        config=ContextManagerConfig(microcompact_trigger_threshold=10, microcompact_keep_recent=5),
        compactable=COMPACTABLE,
    )
    await cm.add_batch(make_pairs(8, name="Read"))  # exactly ceil(10*0.8)
    assert cm.fold_state().near_fold is True


@pytest.mark.asyncio
async def test_fold_state_ignores_non_reconstructable_results():
    # Empty compactable set => no result is foldable => count stays 0.
    cm = ContextManager(
        model="gpt-4",
        config=ContextManagerConfig(microcompact_trigger_threshold=10, microcompact_keep_recent=5),
        compactable=frozenset(),
    )
    await cm.add_batch(make_pairs(8, name="Read"))
    assert cm.fold_state().active_count == 0


# ---------------------------------------------------------------------------
# manage_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_history_empty_returns_false():
    cm = ContextManager(model="gpt-4")
    assert await cm.manage_history() is False


@pytest.mark.asyncio
async def test_manage_history_microcompact_only():
    # No llm → only the cheap pass runs. 4 Read pairs over trigger=2 fold.
    ctx = LLMCallContext()
    cm = ContextManager(ctx, config=MICRO_CFG, model="gpt-4", compactable=COMPACTABLE)
    await cm.add_batch(make_pairs(4))
    changed = await cm.manage_history()
    assert changed is True
    cleared = [m for m in ctx.messages if m.content == "[Old tool result content cleared]"]
    assert len(cleared) == 3


@pytest.mark.asyncio
async def test_manage_history_no_trigger_returns_false():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    await cm.add_batch(make_pairs(2))  # below microcompact trigger, no autocompact llm
    assert await cm.manage_history() is False


@pytest.mark.asyncio
async def test_manage_history_runs_autocompact(force_autocompact_threshold):
    ctx = LLMCallContext()
    cfg = ContextManagerConfig(
        enable_microcompact=False,
        keep_tail_messages=1,
        keep_tail_tokens=1,
    )
    llm = FakeLLM(summary="<summary>compacted</summary>")
    cm = ContextManager(ctx, llm=llm, config=cfg, model="m")
    await cm.add_batch([text_msg(f"turn {i} content here") for i in range(6)])
    changed = await cm.manage_history()
    assert changed is True
    # history replaced by [summary] + tail, swapped into the backing context
    assert len(ctx.messages) == 2
    assert "compacted" in ctx.messages[0].content


@pytest.mark.asyncio
async def test_manage_history_reprojects_sticky_after_compaction(force_autocompact_threshold):
    # A wired sticky_provider re-inserts loaded bodies right after the summary
    # when autocompact discards the head.
    ctx = LLMCallContext()
    cfg = ContextManagerConfig(
        enable_microcompact=False,
        keep_tail_messages=1,
        keep_tail_tokens=1,
    )
    llm = FakeLLM(summary="<summary>compacted</summary>")
    cm = ContextManager(
        ctx,
        llm=llm,
        config=cfg,
        model="m",
        sticky_provider=lambda: [text_msg("STICKY SKILL BODY", role="user")],
    )
    await cm.add_batch([text_msg(f"turn {i} content here") for i in range(6)])
    assert await cm.manage_history() is True
    # [summary, sticky, tail]
    assert len(ctx.messages) == 3
    assert "compacted" in ctx.messages[0].content
    assert ctx.messages[1].content == "STICKY SKILL BODY"


@pytest.mark.asyncio
async def test_manage_history_threads_failure_counter(force_autocompact_threshold):
    cfg = ContextManagerConfig(
        enable_microcompact=False,
        keep_tail_messages=1,
        keep_tail_tokens=1,
        max_consecutive_failures=5,
    )
    llm = FakeLLM(raise_exc=RuntimeError("nope"))
    cm = ContextManager(llm=llm, config=cfg, model="m")
    await cm.add_batch([text_msg(f"turn {i}") for i in range(6)])
    await cm.manage_history()
    assert cm._consecutive_failures == 1
    await cm.manage_history()
    assert cm._consecutive_failures == 2


# ---------------------------------------------------------------------------
# prepare_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_request_appends_prompt_without_storing():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    await cm.add(text_msg("history"))
    req = await cm.prepare_request("the new prompt")
    assert [m.content for m in req] == ["history", "the new prompt"]
    assert isinstance(req[-1], UserMessage)
    # the prompt is in the request but NOT in the stored history
    assert cm.count() == 1


@pytest.mark.asyncio
async def test_prepare_request_accepts_message_object():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    await cm.add(text_msg("history"))
    prompt_msg = UserMessage(content="prebuilt")
    req = await cm.prepare_request(prompt_msg)
    assert req[-1] is prompt_msg


@pytest.mark.asyncio
async def test_prepare_request_none_prompt_returns_history_copy():
    cm = ContextManager(config=MICRO_CFG, model="gpt-4")
    await cm.add(text_msg("history"))
    req = await cm.prepare_request(None)
    assert [m.content for m in req] == ["history"]
    # a fresh list — mutating it does not touch the store
    req.append(text_msg("scratch"))
    assert cm.count() == 1


@pytest.mark.asyncio
async def test_prepare_request_manage_false_skips_compaction():
    ctx = LLMCallContext()
    cm = ContextManager(ctx, config=MICRO_CFG, model="gpt-4")
    await cm.add_batch(make_pairs(4))  # would fold if management ran
    req = await cm.prepare_request("prompt", manage=False)
    assert all(m.content != "[Old tool result content cleared]" for m in ctx.messages)
    assert req[-1].content == "prompt"


# ---------------------------------------------------------------------------
# React-unit delete (direct history editing)
# ---------------------------------------------------------------------------
from mote.common.schema import AIMessage  # noqa: E402


class _RecordingBus:
    """Minimal event bus double — records every emitted event for assertions.

    ``subscribe`` is a no-op (the manager registers itself to observe
    ``ToolsChangedEvent``); ``emit`` records so tests can assert on the stream.
    """

    def __init__(self):
        self.emitted: list = []

    def subscribe(self, subscriber) -> None:
        pass

    async def emit(self, event) -> None:
        self.emitted.append(event)


def _human(content: str) -> UserMessage:
    """A human prompt (a react-unit anchor)."""
    return UserMessage(content=content)


def _reminder() -> UserMessage:
    """A role=user <system-reminder> envelope — NOT a react-unit boundary."""
    from mote.common.text import wrap_system_reminder

    return UserMessage(content=wrap_system_reminder(["injected context"]))


def _drop(messages, anchor_ids):
    """Run the pure boundary helper with the real human-prompt predicate."""
    return ContextManager._react_unit_drop_indices(messages, anchor_ids, ContextManager._is_human_prompt)


def test_drop_indices_single_unit():
    """One anchor drops its prompt + reply + tool turns, up to the next prompt."""
    msgs = [_human("q1"), AIMessage(content="a1"), _human("q2"), AIMessage(content="a2")]
    assert _drop(msgs, [msgs[0].id]) == {0, 1}


def test_drop_indices_last_unit_runs_to_end():
    """The final react-unit has no following prompt — drops to end of history."""
    msgs = [_human("q1"), AIMessage(content="a1"), _human("q2"), AIMessage(content="a2")]
    assert _drop(msgs, [msgs[2].id]) == {2, 3}


def test_drop_indices_adjacent_units():
    """Two selected adjacent anchors drop both whole turns."""
    msgs = [_human("q1"), AIMessage(content="a1"), _human("q2"), AIMessage(content="a2")]
    assert _drop(msgs, [msgs[0].id, msgs[2].id]) == {0, 1, 2, 3}


def test_drop_indices_reminder_is_not_a_boundary():
    """A <system-reminder> role=user message stays inside the anchor's unit."""
    msgs = [_human("q1"), _reminder(), AIMessage(content="a1"), _human("q2")]
    # The reminder (idx 1) and reply (idx 2) belong to q1's unit; q2 ends it.
    assert _drop(msgs, [msgs[0].id]) == {0, 1, 2}


def test_drop_indices_unknown_id_is_ignored():
    msgs = [_human("q1"), AIMessage(content="a1")]
    assert _drop(msgs, ["no-such-id"]) == set()


def test_drop_indices_empty_selection():
    msgs = [_human("q1"), AIMessage(content="a1")]
    assert _drop(msgs, []) == set()


def test_drop_indices_delete_all():
    msgs = [_human("q1"), AIMessage(content="a1"), _human("q2"), AIMessage(content="a2")]
    assert _drop(msgs, [msgs[0].id, msgs[2].id]) == {0, 1, 2, 3}


@pytest.mark.asyncio
async def test_delete_react_units_prunes_and_emits_one_event():
    """A delete rebuilds history once and emits exactly one HistoryEditedEvent."""
    from mote.common.events import HistoryEditedEvent

    ctx = LLMCallContext()
    bus = _RecordingBus()
    cm = ContextManager(ctx, model="gpt-4", bus=bus)
    q1, a1, q2, a2 = _human("q1"), AIMessage(content="a1"), _human("q2"), AIMessage(content="a2")
    await cm.add_batch([q1, a1, q2, a2])
    bus.emitted.clear()  # ignore the MessageAppended events from add_batch

    removed = await cm.delete_react_units([q1.id])

    assert removed == 2
    assert [m.content for m in cm.get()] == ["q2", "a2"]
    edits = [e for e in bus.emitted if isinstance(e, HistoryEditedEvent)]
    assert len(edits) == 1
    assert [m.content for m in edits[0].messages] == ["q2", "a2"]
    assert edits[0].reason == "delete"


@pytest.mark.asyncio
async def test_delete_react_units_noop_emits_nothing():
    """An empty/unknown selection removes nothing and emits no event."""
    from mote.common.events import HistoryEditedEvent

    ctx = LLMCallContext()
    bus = _RecordingBus()
    cm = ContextManager(ctx, model="gpt-4", bus=bus)
    await cm.add_batch([_human("q1"), AIMessage(content="a1")])
    bus.emitted.clear()

    removed = await cm.delete_react_units(["ghost"])

    assert removed == 0
    assert cm.count() == 2
    assert not [e for e in bus.emitted if isinstance(e, HistoryEditedEvent)]
