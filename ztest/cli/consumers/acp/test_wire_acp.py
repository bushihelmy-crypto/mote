#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``cli/consumers/_wire/acp`` — the pure ViewEvent → ACP mapper.

The mapper is transport-free: given a neutral ``ViewEvent`` + a per-session
:class:`AcpWireState`, it returns zero-or-more ACP ``update`` dicts (the payload
the transport wraps in ``{sessionId, update}`` for a ``session/update``
notification). These tests exercise the wire shapes directly — no stdio, no
JSON-RPC — asserting the ``sessionUpdate`` discriminator, tool kind/status, file
diff round-trip, and the permission option vocabulary.
"""

from __future__ import annotations

from mote.contracts.tool import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
from mote.product.interfaces.acp import wire as acp
from mote.product.presentation.events import events as ev


def _identity(value: str) -> ToolInvocationIdentity:
    return ToolInvocationIdentity(
        ToolInvocationId(value), ToolAttemptOrdinal(1), "definition", 1, "digest", "owner", "run"
    )


def _state() -> acp.AcpWireState:
    return acp.AcpWireState(session_id="sess-1")


# ── message chunks ──────────────────────────────────────────────────────────
def test_streaming_message_deltas_share_one_message_id():
    st = _state()
    assert acp.to_acp_updates(ev.MessageBlockStarted(role="assistant"), st) == []
    a = acp.to_acp_updates(ev.MessageBlockDelta(text="Hel"), st)
    b = acp.to_acp_updates(ev.MessageBlockDelta(text="lo"), st)
    assert a[0]["sessionUpdate"] == acp.AGENT_MESSAGE_CHUNK
    assert a[0]["content"] == {"type": "text", "text": "Hel"}
    # both deltas ride the same minted messageId
    assert a[0]["messageId"] == b[0]["messageId"]
    # completing a streamed block emits nothing (deltas already carried it)
    done = acp.to_acp_updates(ev.MessageBlockCompleted(role="assistant", markdown="Hello", streamed=True), st)
    assert done == []


def test_non_streamed_block_emits_whole_markdown_once():
    st = _state()
    out = acp.to_acp_updates(ev.MessageBlockCompleted(role="assistant", markdown="whole", streamed=False), st)
    assert len(out) == 1
    assert out[0]["sessionUpdate"] == acp.AGENT_MESSAGE_CHUNK
    assert out[0]["content"]["text"] == "whole"


def test_reasoning_delta_maps_to_thought_chunk():
    st = _state()
    out = acp.to_acp_updates(ev.ReasoningDelta(text="thinking"), st)
    assert out[0]["sessionUpdate"] == acp.AGENT_THOUGHT_CHUNK
    assert out[0]["content"]["text"] == "thinking"


# ── tool calls: kind + status ────────────────────────────────────────────────
def test_tool_started_announces_full_call_with_kind():
    st = _state()
    out = acp.to_acp_updates(ev.ToolCallStarted(identity=_identity("tc-1"), tool_name="Bash", headline="ls -la"), st)
    call = out[0]
    assert call["sessionUpdate"] == acp.TOOL_CALL
    assert call["toolCallId"] == "tc-1"
    assert call["kind"] == acp.TOOL_KIND_EXECUTE  # Bash → execute
    assert call["status"] == acp.STATUS_IN_PROGRESS
    assert call["title"] == "Bash"  # title falls back to tool_name (no explicit title)
    assert call["content"][0]["content"]["text"] == "ls -la"  # headline → content preview
    assert "tc-1" in st.seen_tools


def test_tool_completed_after_start_is_partial_update():
    st = _state()
    acp.to_acp_updates(ev.ToolCallStarted(identity=_identity("tc-2"), tool_name="Read"), st)
    out = acp.to_acp_updates(ev.ToolCallCompleted(identity=_identity("tc-2"), ok=True, summary="read 10 lines"), st)
    upd = out[0]
    assert upd["sessionUpdate"] == acp.TOOL_CALL_UPDATE  # start seen → partial
    assert upd["toolCallId"] == "tc-2"
    assert upd["status"] == acp.STATUS_COMPLETED
    assert upd["content"][0]["content"]["text"] == "read 10 lines"


def test_tool_completed_without_start_is_promoted_to_full_call():
    st = _state()
    out = acp.to_acp_updates(ev.ToolCallCompleted(identity=_identity("tc-3"), ok=False, summary="boom"), st)
    upd = out[0]
    # never announced → promote to a full tool_call so the editor learns kind/title
    assert upd["sessionUpdate"] == acp.TOOL_CALL
    assert upd["status"] == acp.STATUS_FAILED
    assert "kind" in upd and "title" in upd


def test_tool_kind_classification_defaults_to_other():
    assert acp.tool_kind_for("Read") == acp.TOOL_KIND_READ
    assert acp.tool_kind_for("Edit") == acp.TOOL_KIND_EDIT
    assert acp.tool_kind_for("Search") == acp.TOOL_KIND_SEARCH
    assert acp.tool_kind_for("WebSearch") == acp.TOOL_KIND_FETCH
    assert acp.tool_kind_for("Nonexistent") == acp.TOOL_KIND_OTHER


# ── file diff: locations + create/delete conventions ─────────────────────────
def test_file_diff_carries_diff_block_and_locations():
    st = _state()
    out = acp.to_acp_updates(ev.FileDiffBlock(identity=_identity("tc-e"), path="a.py", old="x", new="y"), st)
    upd = out[0]
    diff = upd["content"][0]
    assert diff["type"] == "diff"
    assert diff["path"] == "a.py"
    assert diff["oldText"] == "x"
    assert diff["newText"] == "y"
    assert upd["locations"] == [{"path": "a.py"}]


def test_file_diff_creation_has_null_old_text():
    st = _state()
    out = acp.to_acp_updates(ev.FileDiffBlock(identity=_identity("tc-c"), path="new.py", old="", new="created"), st)
    # empty old → creation → oldText null per ACP convention
    assert out[0]["content"][0]["oldText"] is None
    assert out[0]["content"][0]["newText"] == "created"


def test_file_diff_before_tool_start_announces_edit_call():
    st = _state()
    out = acp.to_acp_updates(ev.FileDiffBlock(identity=_identity("tc-z"), path="z.py", old="a", new="b"), st)
    # no prior tool_call for tc-z → announce a minimal edit call
    assert out[0]["sessionUpdate"] == acp.TOOL_CALL
    assert out[0]["kind"] == acp.TOOL_KIND_EDIT
    assert out[0]["status"] == acp.STATUS_IN_PROGRESS


# ── permission vocabulary ────────────────────────────────────────────────────
def test_permission_options_are_the_four_kinds():
    opts = acp.permission_options()
    ids = [o["optionId"] for o in opts]
    assert ids == [
        acp.PERM_ALLOW_ONCE,
        acp.PERM_ALLOW_ALWAYS,
        acp.PERM_REJECT_ONCE,
        acp.PERM_REJECT_ALWAYS,
    ]
    # each optionId equals its kind so the port maps straight back
    assert all(o["optionId"] == o["kind"] for o in opts)


def test_perm_kind_maps_to_outcome():
    assert acp.PERM_KIND_TO_OUTCOME[acp.PERM_ALLOW_ONCE] == "accept"
    assert acp.PERM_KIND_TO_OUTCOME[acp.PERM_ALLOW_ALWAYS] == "always_allow"
    assert acp.PERM_KIND_TO_OUTCOME[acp.PERM_REJECT_ONCE] == "reject"
    assert acp.PERM_KIND_TO_OUTCOME[acp.PERM_REJECT_ALWAYS] == "always_deny"


def test_tool_call_update_for_permission_carries_kind_and_title():
    upd = acp.tool_call_update_for_permission(tool_call_id="tc-p", tool_name="Bash", title="rm -rf /")
    assert upd["toolCallId"] == "tc-p"
    assert upd["status"] == acp.STATUS_PENDING
    assert upd["kind"] == acp.TOOL_KIND_EXECUTE
    assert upd["title"] == "rm -rf /"


# ── unknown / display-only kinds → [] ────────────────────────────────────────
def test_unknown_kind_maps_to_empty():
    st = _state()

    class Bogus:
        kind = "not_real"

    assert acp.to_acp_updates(Bogus(), st) == []


def test_notice_and_error_ride_agent_text():
    st = _state()
    assert acp.to_acp_updates(ev.Notice(text="fyi"), st)[0]["content"]["text"] == "fyi"
    err = acp.to_acp_updates(ev.ErrorRaised(text="bad"), st)
    assert "bad" in err[0]["content"]["text"]


def test_output_snapshot_and_invalidation_use_typed_extensions():
    st = _state()
    snapshot = acp.to_acp_updates(
        ev.OutputSnapshot(
            run_id="run-1",
            revision=1,
            schema_fingerprint="sha",
            value={"count": 7},
        ),
        st,
    )[0]
    invalidated = acp.to_acp_updates(
        ev.OutputSnapshotInvalidated(run_id="run-1", revision=1, reason="stream_changed"),
        st,
    )[0]

    assert snapshot["sessionUpdate"] == "mote_output_snapshot"
    assert snapshot["value"] == {"count": 7}
    assert invalidated["sessionUpdate"] == "mote_output_snapshot_invalidated"


def test_committed_output_is_typed_terminal_extension():
    output = acp.to_acp_updates(
        ev.OutputCommitted(
            run_id="run-1",
            contract_id="test.report@1",
            schema_fingerprint="sha",
            value={"count": 7},
        ),
        _state(),
    )[0]

    assert output["sessionUpdate"] == "mote_output_committed"
    assert output["runId"] == "run-1"
