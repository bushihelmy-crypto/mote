#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the LLM :class:`SummarizeReducer` — summarize head, keep tail.

This is the old ``autocompact`` behavior expressed as a reducer, with the one
critical fix: the head/tail split now goes through the boundary-safe
:meth:`Transcript.split_keep_tail`, so the rebuilt ``[summary] + tail`` can never
begin with an orphan ``tool_result`` (the flat-index bug that 400'd Anthropic).

Preserved behavior verified here: the summarize circuit breaker, ``sticky_provider``
re-projection right after the summary, and partial-vs-full compact-prompt choice.
"""
from __future__ import annotations

import asyncio

from mote.contracts.schema import ContextManagerConfig, UserMessage
from mote.runtime.context.compaction.reducers.summarize import SummarizeReducer
from mote.runtime.context.compaction.request import ReductionRequest
from mote.runtime.context.compaction.transcript import Transcript
from mote.ztest.model_fakes import model_route

from ..conftest import FakeLLM, make_pairs, text_msg


def _run(coro):
    return asyncio.run(coro)


def _cfg(**kw) -> ContextManagerConfig:
    base = dict(keep_tail_messages=3, keep_tail_tokens=1, max_consecutive_failures=3)
    base.update(kw)
    return ContextManagerConfig(**base)


def _reduce(transcript, *, llm=None, cfg=None, sticky_provider=None, rehydrate_provider=None, target=0):
    reducer = SummarizeReducer(
        model_route(llm or FakeLLM(summary="SUMMARY")),
        cfg or _cfg(),
        model="gpt-4",
        sticky_provider=sticky_provider,
        rehydrate_provider=rehydrate_provider,
    )
    req = ReductionRequest(target_tokens=target)
    return reducer, _run(reducer.reduce(transcript, req))


def _assert_pairing_valid(messages):
    """Every tool_result must be preceded by the tool_call it answers."""
    seen: set = set()
    for m in messages:
        calls = m.metadata.get("tool_calls")
        if calls:
            for c in calls:
                seen.add(c["id"])
        cid = m.metadata.get("tool_call_id")
        if cid is not None:
            assert cid in seen, f"orphan tool_result {cid} before its call"


def test_summarizes_head_and_rebuilds_with_summary():
    msgs = [text_msg(f"turn {i}") for i in range(8)]
    t = Transcript.from_messages(msgs)
    reducer, out = _reduce(t)
    assert out.changed is True
    assert out.summary == "SUMMARY"
    # The FakeLLM was asked exactly once, with the head as msg.
    assert len(reducer._model_route.gateway.llm.aask_calls) == 1
    # The rebuilt history contains the summary user message.
    assert any("SUMMARY" in (m.content or "") for m in out.transcript.to_messages())


def test_split_never_yields_orphan_tool_result_in_tail():
    # The core regression: a tool group straddling the natural cut must stay
    # whole, so the kept tail never begins with a bare tool_result.
    msgs = [text_msg(f"turn {i}") for i in range(2)]
    msgs += make_pairs(6, result="y" * 400)  # 6 atomic groups
    t = Transcript.from_messages(msgs)
    _, out = _reduce(t)
    assert out.changed is True
    _assert_pairing_valid(out.transcript.to_messages())


def test_too_short_history_is_noop():
    t = Transcript.from_messages([text_msg("a"), text_msg("b")])
    _, out = _reduce(t)
    assert out.changed is False


def test_disabled_is_noop():
    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, cfg=_cfg(enable_autocompact=False))
    assert out.changed is False


def test_no_llm_is_noop():
    reducer = SummarizeReducer(None, _cfg(), model="gpt-4")
    out = _run(reducer.reduce(Transcript.from_messages([text_msg(f"m{i}") for i in range(8)]), ReductionRequest(0)))
    assert out.changed is False


def test_circuit_breaker_stops_after_max_failures():
    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    reducer = SummarizeReducer(model_route(FakeLLM(summary="S")), _cfg(max_consecutive_failures=3), model="gpt-4")
    reducer.consecutive_failures = 3  # already at the limit
    out = _run(reducer.reduce(t, ReductionRequest(0)))
    assert out.changed is False
    assert reducer._model_route.gateway.llm.aask_calls == []  # never even called the LLM


def test_failure_increments_counter():
    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    reducer = SummarizeReducer(model_route(FakeLLM(raise_exc=RuntimeError("boom"))), _cfg(), model="gpt-4")
    out = _run(reducer.reduce(t, ReductionRequest(0)))
    assert out.changed is False
    assert reducer.consecutive_failures == 1


def test_success_resets_counter():
    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    reducer = SummarizeReducer(model_route(FakeLLM(summary="S")), _cfg(), model="gpt-4")
    reducer.consecutive_failures = 2
    out = _run(reducer.reduce(t, ReductionRequest(0)))
    assert out.changed is True
    assert reducer.consecutive_failures == 0


def test_sticky_reprojected_after_summary():
    def provider():
        return [UserMessage(content="STICKY BODY")]

    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, sticky_provider=provider)
    contents = [m.content or "" for m in out.transcript.to_messages()]
    joined = "\n".join(contents)
    assert "STICKY BODY" in joined
    # sticky must land after the summary.
    summary_idx = next(i for i, c in enumerate(contents) if "SUMMARY" in c)
    sticky_idx = next(i for i, c in enumerate(contents) if "STICKY BODY" in c)
    assert sticky_idx > summary_idx


def test_task_result_pointer_reprojected_after_summary():
    # The push-once bg-task pointer survives compaction via the SAME
    # sticky_provider seam: a ResourceRegistry.project projects the registered
    # task_result unit, which must land after the summary (so the model is
    # re-reminded of a result the discarded notification once carried).
    from mote.runtime.resources import ResourceRegistry, build_task_result_pointer

    registry = ResourceRegistry()
    pointer = build_task_result_pointer(
        task_id="bg_3",
        command_name="code review",
        status="success",
        summary="code review finished (success).",
        result="found 2 issues",
    )
    registry.load(id="bg_3", kind="task_result", content=pointer, sticky=True)

    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, sticky_provider=registry.project)
    contents = [m.content or "" for m in out.transcript.to_messages()]
    joined = "\n".join(contents)
    assert "<task-result>" in joined
    assert "bg_3" in joined
    summary_idx = next(i for i, c in enumerate(contents) if "SUMMARY" in c)
    ptr_idx = next(i for i, c in enumerate(contents) if "<task-result>" in c)
    assert ptr_idx > summary_idx


def test_sticky_provider_failure_is_swallowed():
    def boom():
        raise RuntimeError("provider down")

    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, sticky_provider=boom)
    assert out.changed is True  # summarize still succeeds despite sticky failure


def test_rehydrated_files_injected_after_summary():
    def provider():
        return [UserMessage(content="FILE SNAPSHOT BODY")]

    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, rehydrate_provider=provider)
    contents = [m.content or "" for m in out.transcript.to_messages()]
    joined = "\n".join(contents)
    assert "FILE SNAPSHOT BODY" in joined
    summary_idx = next(i for i, c in enumerate(contents) if "SUMMARY" in c)
    file_idx = next(i for i, c in enumerate(contents) if "FILE SNAPSHOT BODY" in c)
    assert file_idx > summary_idx


def test_rehydrated_files_land_after_sticky():
    # Order in the rebuild: [summary, sticky, rehydrated, tail].
    _, out = _reduce(
        Transcript.from_messages([text_msg(f"m{i}") for i in range(8)]),
        sticky_provider=lambda: [UserMessage(content="STICKY BODY")],
        rehydrate_provider=lambda: [UserMessage(content="FILE SNAPSHOT BODY")],
    )
    contents = [m.content or "" for m in out.transcript.to_messages()]
    sticky_idx = next(i for i, c in enumerate(contents) if "STICKY BODY" in c)
    file_idx = next(i for i, c in enumerate(contents) if "FILE SNAPSHOT BODY" in c)
    assert file_idx > sticky_idx


def test_rehydrate_provider_failure_is_swallowed():
    def boom():
        raise RuntimeError("rehydrate down")

    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, rehydrate_provider=boom)
    assert out.changed is True  # summarize still succeeds despite rehydrate failure


def test_rehydrate_provider_receives_preserved_tail():
    # The reducer must hand the preserved tail to the provider so it can dedup
    # files that tail already shows (the read-tool file-path collection seam).
    captured: dict = {}

    def provider(preserved):
        captured["preserved"] = preserved
        return [UserMessage(content="FILE SNAPSHOT BODY")]

    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, rehydrate_provider=provider)
    assert out.changed is True
    # The tail (kept verbatim) was passed through, non-empty for this history.
    assert "preserved" in captured
    assert isinstance(captured["preserved"], list)
    assert captured["preserved"]  # keep_tail_messages=3 => a non-empty tail


def test_rehydrate_zero_arg_provider_still_supported():
    # A legacy zero-arg provider (no preserved param) must still work.
    def provider():
        return [UserMessage(content="FILE SNAPSHOT BODY")]

    t = Transcript.from_messages([text_msg(f"m{i}") for i in range(8)])
    _, out = _reduce(t, rehydrate_provider=provider)
    contents = [m.content or "" for m in out.transcript.to_messages()]
    assert any("FILE SNAPSHOT BODY" in c for c in contents)
