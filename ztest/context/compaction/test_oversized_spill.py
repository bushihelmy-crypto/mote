#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the FREE :class:`OversizedSpillReducer` — lossless spill of runaway parts.

The per-tool size cap only sees a *tool's output* at the executor chokepoint.
This reducer catches the two classes of oversized content that land straight in
history — a runaway assistant/user body and a giant tool-call ``args`` blob — plus
stray oversized results loaded from a resumed session. It reuses the tool path's
``enforce_tool_result_limit`` primitive (persist + ``<persisted-output>`` pointer,
routed through the session's :class:`WorkspaceStore`), so it is lossless and FREE:
the full content lands on disk and the in-history part names the file.

Disk writes are pointed at ``tmp_path`` via a ``WorkspaceStore`` rooted there, so a
spilled part co-locates under the session directory
(``.agent_sessions/{session}/tool_results/{id}.txt``).
"""
from __future__ import annotations

import asyncio
import json

from mote.common.const import RETENTION, RETENTION_PIN, TOOL_CALLS
from mote.common.schema import PERSISTED_OUTPUT_OPEN_TAG, ContextManagerConfig, Message, ToolResultLimitConfig
from mote.common.workspace import ArtifactKind, WorkspaceStore
from mote.context.compaction.pipeline import ReductionPipeline
from mote.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.context.compaction.reducers.spill import OversizedSpillReducer
from mote.context.compaction.request import ReductionRequest
from mote.context.compaction.transcript import Transcript

from ..conftest import COMPACTABLE, text_msg, tool_call_msg, tool_result_msg

SESSION = "sess"
BIG = "A" * 5000


def _run(coro):
    return asyncio.run(coro)


def _reducer(tmp_path, *, threshold: int = 200, persist: bool = True, enable: bool = True) -> OversizedSpillReducer:
    return OversizedSpillReducer(
        ContextManagerConfig(),
        model="gpt-4",
        session_id=SESSION,
        store=WorkspaceStore(tmp_path),
        limit_config=ToolResultLimitConfig(
            enable_tool_result_limit=enable,
            persist_large_tool_results=persist,
            default_max_result_size_chars=threshold,
        ),
    )


def _spilled_path(tmp_path, result_id: str):
    return WorkspaceStore(tmp_path).space(SESSION, ArtifactKind.TOOL_RESULTS) / f"{result_id}.txt"


def _reduce(reducer, msgs, *, target=10_000_000):
    transcript = Transcript.from_messages(msgs, compactable=COMPACTABLE)
    return _run(reducer.reduce(transcript, ReductionRequest(target_tokens=target)))


def test_oversized_message_content_spilled_to_disk(tmp_path):
    msg = text_msg(BIG, role="assistant")
    out = _reduce(_reducer(tmp_path), [msg])

    assert out.changed is True
    assert out.strategy == "spill"
    assert out.tokens_freed > 0
    # In-history content is now the envelope naming the on-disk file.
    assert msg.content.startswith(PERSISTED_OUTPUT_OPEN_TAG)
    # Full body on disk under the session's tool_results/, named by the msg id.
    path = _spilled_path(tmp_path, msg.id)
    assert path.exists()
    assert path.read_text() == BIG


def test_oversized_tool_call_args_spilled_to_disk(tmp_path):
    big_args = {"blob": "B" * 5000}
    # Assistant content is empty here, so only the args are oversized.
    msg = tool_call_msg("call-1", "Bash", args=big_args)
    out = _reduce(_reducer(tmp_path), [msg])

    assert out.changed is True
    call = msg.metadata[TOOL_CALLS][0]
    assert isinstance(call["args"], str)
    assert call["args"].startswith(PERSISTED_OUTPUT_OPEN_TAG)
    # Full serialized args JSON on disk under the "{id}-args" namespace.
    path = _spilled_path(tmp_path, "call-1-args")
    assert path.exists()
    assert path.read_text() == json.dumps(big_args)


def test_under_threshold_content_unchanged(tmp_path):
    msg = text_msg("small", role="assistant")
    out = _reduce(_reducer(tmp_path), [msg])

    assert out.changed is False
    assert msg.content == "small"
    assert not (tmp_path / ".agent_sessions").exists()


def test_already_persisted_content_is_idempotent(tmp_path):
    wrapped = f"{PERSISTED_OUTPUT_OPEN_TAG}\nalready\n</persisted-output>" + "y" * 5000
    msg = text_msg(wrapped, role="assistant")
    out = _reduce(_reducer(tmp_path), [msg])

    # Over threshold but already wrapped → left alone (prefix-stable across turns).
    assert out.changed is False
    assert msg.content == wrapped


def test_system_anchor_segment_is_skipped(tmp_path):
    sysmsg = Message(content="S" * 5000, role="system")
    out = _reduce(_reducer(tmp_path), [sysmsg])

    assert out.changed is False
    assert sysmsg.content == "S" * 5000


def test_retention_pinned_result_is_skipped(tmp_path):
    call = tool_call_msg("c", "Bash")
    res = tool_result_msg("c", "R" * 5000)
    res.add_metadata(RETENTION, RETENTION_PIN)
    out = _reduce(_reducer(tmp_path), [call, res])

    assert out.changed is False
    assert res.content == "R" * 5000


def test_persist_disabled_truncates_inline_without_file(tmp_path):
    msg = text_msg("Q" * 5000, role="assistant")
    out = _reduce(_reducer(tmp_path, persist=False), [msg])

    assert out.changed is True
    # Inline truncation, not a pointer — and shrunk.
    assert not msg.content.startswith(PERSISTED_OUTPUT_OPEN_TAG)
    assert len(msg.content) < 5000
    assert not (tmp_path / ".agent_sessions").exists()


def test_disabled_limit_is_noop(tmp_path):
    msg = text_msg(BIG, role="assistant")
    out = _reduce(_reducer(tmp_path, enable=False), [msg])

    assert out.changed is False
    assert msg.content == BIG


def test_cost_is_free():
    assert OversizedSpillReducer.cost == ReducerCost.FREE
    assert ReducerCost.FREE < ReducerCost.LLM


def test_runs_before_summarize_in_pipeline(tmp_path):
    """Cost-sort places the FREE spill before an LLM summarize, and once spill
    brings the transcript under target the pipeline stops without summarizing."""

    class _FakeSummarize:
        cost = ReducerCost.LLM

        def __init__(self) -> None:
            self.ran = False

        async def reduce(self, transcript, request) -> ReductionOutcome:
            self.ran = True
            return ReductionOutcome(transcript, strategy="summarize")

    fake = _FakeSummarize()
    # Pass the reducers out of order to prove the pipeline sorts by cost.
    pipeline = ReductionPipeline([fake, _reducer(tmp_path)], model="gpt-4")
    transcript = Transcript.from_messages([text_msg(BIG, role="assistant")], compactable=COMPACTABLE)
    out = _run(pipeline.run(transcript, ReductionRequest(target_tokens=10_000_000)))

    assert out.changed is True
    assert out.strategy == "spill"
    assert fake.ran is False
