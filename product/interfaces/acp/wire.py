#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``ViewEvent → ACP session/update`` — the pure mapper (Phase 4 core asset).

ACP (Agent Client Protocol, https://agentclientprotocol.com) is Zed's stdio
JSON-RPC protocol an editor speaks to an agent. Where AG-UI streams SSE frames,
ACP streams ``session/update`` **notifications** — each carries an ``update``
object tagged by a ``sessionUpdate`` discriminator. This module is the
**transport-free** translation table from mote's neutral ``ViewEvent`` spine to
those ``update`` payloads: no stdio, no JSON-RPC framing, no ACP endpoint. The
ACP consumer (``consumers/acp/``) owns the JSON-RPC transport and calls
:func:`to_acp_updates` to serialize; every wire-shape decision lives *here* so it
is unit-testable in isolation and swappable without touching transport.

Design notes
------------
* **Fan-out, not 1:1.** One ViewEvent can map to several ACP updates (a tool
  completion → a ``tool_call_update`` carrying status + content), so the mapper
  returns a ``list[dict]`` (possibly empty). An unknown / display-only kind →
  ``[]`` (a forward-compatible client simply never sees it — never an error).
  Each returned dict is the ``update`` object; the transport wraps it in
  ``{sessionId, update}`` and sends it as a ``session/update`` notification.
* **Id correlation.** ACP keys streaming text by ``messageId`` (all chunks of one
  assistant message share it) and tool calls by ``toolCallId``. mote already
  carries execution-owner invocation identity; for message blocks (no per-block id on
  the wire until completion) the mapper mints a stable per-session ``messageId``
  from a monotonic block counter. :class:`AcpWireState` holds that tiny
  correlation state so the mapper functions stay pure w.r.t. their inputs.
* **ACP update vocabulary** (field ``sessionUpdate``, snake_case, verified against
  the v1 Rust schema): ``agent_message_chunk`` / ``agent_thought_chunk`` /
  ``user_message_chunk`` (→ ``{content: ContentBlock, messageId?}``),
  ``tool_call`` (→ full ToolCall), ``tool_call_update`` (→ partial by
  ``toolCallId``), ``plan`` (→ ``{entries:[...]}``). ContentBlock is tagged by
  ``type`` (``text`` → ``{type:"text", text}``). ToolKind / ToolCallStatus /
  PlanEntry* are snake_case enums. Kinds without a native ACP shape (usage,
  notice, retry, compaction, activity) ride ``agent_message_chunk`` text (or are
  dropped) so no bespoke client handler is required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional, Set

from mote.contracts.async_work.codec import decode_async_work_observation, encode_async_work_observation
from mote.product.presentation.events import events as ev
from mote.product.presentation.events.catalog import ViewEventDisposition, adapter_disposition
from mote.product.presentation.wire_types import WireObject, to_wire_json

# ── ACP ``sessionUpdate`` discriminators (snake_case per the v1 schema) ─────
USER_MESSAGE_CHUNK = "user_message_chunk"
AGENT_MESSAGE_CHUNK = "agent_message_chunk"
AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
TOOL_CALL = "tool_call"
TOOL_CALL_UPDATE = "tool_call_update"
PLAN = "plan"

# ── ToolKind (snake_case; default ``other``) ────────────────────────────────
TOOL_KIND_READ = "read"
TOOL_KIND_EDIT = "edit"
TOOL_KIND_DELETE = "delete"
TOOL_KIND_MOVE = "move"
TOOL_KIND_SEARCH = "search"
TOOL_KIND_EXECUTE = "execute"
TOOL_KIND_THINK = "think"
TOOL_KIND_FETCH = "fetch"
TOOL_KIND_SWITCH_MODE = "switch_mode"
TOOL_KIND_OTHER = "other"

# ── ToolCallStatus (snake_case; default ``pending``) ────────────────────────
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Map a mote tool name → ACP ToolKind. The editor renders a per-kind icon +
# affordance (a ``read`` shows a file peek, an ``edit`` shows a diff, an
# ``execute`` shows a terminal). Unknown tools fall through to ``other``.
_TOOL_KIND: dict[str, str] = {
    "Read": TOOL_KIND_READ,
    "Edit": TOOL_KIND_EDIT,
    "Write": TOOL_KIND_EDIT,
    "MultiEdit": TOOL_KIND_EDIT,
    "Search": TOOL_KIND_SEARCH,
    "Grep": TOOL_KIND_SEARCH,
    "Glob": TOOL_KIND_SEARCH,
    "Bash": TOOL_KIND_EXECUTE,
    "Terminal": TOOL_KIND_EXECUTE,
    "Python": TOOL_KIND_EXECUTE,
    "Jupyter": TOOL_KIND_EXECUTE,
    "RunGraph": TOOL_KIND_EXECUTE,
    "WebSearch": TOOL_KIND_FETCH,
    "WebBrowser": TOOL_KIND_FETCH,
    "WebFetch": TOOL_KIND_FETCH,
    "Agent": TOOL_KIND_THINK,
}


def tool_kind_for(tool_name: str) -> str:
    """Classify a mote tool name into an ACP ``ToolKind`` (default ``other``)."""
    return _TOOL_KIND.get(tool_name, TOOL_KIND_OTHER)


@dataclass
class AcpWireState:
    """Per-session correlation state (message ids) for one ACP session.

    One instance per resident ACP session (survives across ``session/prompt``
    turns, unlike AG-UI's per-run state — ACP is a long-lived connection). Holds
    the monotonic block counter used to mint a stable ``messageId`` for an
    assistant message block (mote blocks carry no id on the wire until
    completion) and remembers the currently-open block id so ``delta`` chunks
    reference the same message. Tool calls need no state — they carry canonical
    invocation identity. ``seen_tools`` tracks which ``toolCallId``s have
    had their initial ``tool_call`` announced so a completion emits a
    ``tool_call_update`` (partial) rather than re-announcing.
    """

    session_id: str
    _block_seq: int = field(default=0)
    _open_message_id: Optional[str] = field(default=None)
    seen_tools: Set[str] = field(default_factory=set)

    def open_message(self) -> str:
        """Mint + remember a fresh ``messageId`` for a newly-opened block."""
        self._block_seq += 1
        self._open_message_id = f"{self.session_id}-msg-{self._block_seq}"
        return self._open_message_id

    def current_message(self) -> str:
        """The open block's id (mint one lazily if a delta arrives first)."""
        if self._open_message_id is None:
            return self.open_message()
        return self._open_message_id

    def close_message(self) -> str:
        """Return the open block's id and clear it (block boundary)."""
        mid = self.current_message()
        self._open_message_id = None
        return mid


# ── ContentBlock ``type`` discriminators ────────────────────────────────────
BLOCK_TEXT = "text"
BLOCK_CONTENT = "content"
BLOCK_DIFF = "diff"


# ── ContentBlock builders (tagged by ``type``) ──────────────────────────────
def text_block(text: str) -> WireObject:
    """A ``text`` ContentBlock — the one block every ACP client must render."""
    return {"type": BLOCK_TEXT, "text": text}


def _content_block(text: str) -> WireObject:
    """A ToolCallContent wrapper around a text block (editor call preview)."""
    return {"type": BLOCK_CONTENT, "content": text_block(text)}


def _tool_id(e: ev.ToolCallStarted | ev.ToolCallCompleted | ev.FileDiffBlock) -> str:
    """Project the invocation identity minted by the Tool execution owner."""

    return str(e.identity.invocation_id)


def _promote_to_call(
    update: WireObject,
    st: AcpWireState,
    *,
    title: str,
    kind: str,
    status: Optional[str] = None,
) -> None:
    """Turn a ``tool_call_update`` into a full ``tool_call`` in place.

    Used when a completion / diff arrives without its ``tool_call`` start ever
    being announced (non-streaming upstream) so the editor still learns the
    kind/title. Records the id as seen so a later update stays partial.
    ``status`` is set only when given — a completion keeps its own
    completed/failed status, a diff announces ``in_progress``.
    """
    update["sessionUpdate"] = TOOL_CALL
    update["title"] = title
    update["kind"] = kind
    if status is not None:
        update["status"] = status
    tool_call_id = update["toolCallId"]
    if not isinstance(tool_call_id, str):
        raise TypeError("ACP toolCallId must be a string")
    st.seen_tools.add(tool_call_id)


def _chunk(session_update: str, content: WireObject, message_id: Optional[str] = None) -> WireObject:
    """A ContentChunk update (user / agent / thought), optionally message-keyed."""
    update: WireObject = {"sessionUpdate": session_update, "content": content}
    if message_id is not None:
        update["messageId"] = message_id
    return update


def agent_text(text: str, message_id: Optional[str] = None) -> WireObject:
    """An ``agent_message_chunk`` carrying a text block (the common fallback)."""
    return _chunk(AGENT_MESSAGE_CHUNK, text_block(text), message_id)


def _on_output_snapshot(e: ev.OutputSnapshot, st: AcpWireState) -> list[WireObject]:
    return [
        {
            "sessionUpdate": "mote_output_snapshot",
            "runId": e.run_id,
            "revision": e.revision,
            "schemaFingerprint": e.schema_fingerprint,
            "value": to_wire_json(e.value),
        }
    ]


def _on_output_snapshot_invalidated(e: ev.OutputSnapshotInvalidated, st: AcpWireState) -> list[WireObject]:
    return [
        {
            "sessionUpdate": "mote_output_snapshot_invalidated",
            "runId": e.run_id,
            "revision": e.revision,
            "reason": e.reason,
        }
    ]


def _on_output_committed(e: ev.OutputCommitted, st: AcpWireState) -> list[WireObject]:
    return [
        {
            "sessionUpdate": "mote_output_committed",
            "runId": e.run_id,
            "runKind": e.run_kind,
            "contractId": e.contract_id,
            "schemaFingerprint": e.schema_fingerprint,
            "value": to_wire_json(e.value),
        }
    ]


# ── permission mapping (session/request_permission) ─────────────────────────
# ACP PermissionOptionKind (snake_case) ↔ mote ApprovalDecision.outcome. The
# port builds the option list from these; the client's chosen optionId maps back
# to an outcome. Kept here so the whole permission vocabulary lives beside the
# wire shapes (mirrors agui.approval_prompt living beside the AG-UI shapes).
PERM_ALLOW_ONCE = "allow_once"
PERM_ALLOW_ALWAYS = "allow_always"
PERM_REJECT_ONCE = "reject_once"
PERM_REJECT_ALWAYS = "reject_always"

#: ACP PermissionOptionKind → mote ApprovalDecision.outcome.
PERM_KIND_TO_OUTCOME: dict[str, str] = {
    PERM_ALLOW_ONCE: "accept",
    PERM_ALLOW_ALWAYS: "always_allow",
    PERM_REJECT_ONCE: "reject",
    PERM_REJECT_ALWAYS: "always_deny",
}


def permission_options() -> list[WireObject]:
    """The four ACP ``PermissionOption``s a gated tool call offers the client.

    Stable ``optionId``s (== the kind) so the port maps the chosen id straight
    back to an outcome via :data:`PERM_KIND_TO_OUTCOME`. The editor renders
    ``name`` as the button label and groups by ``kind`` (allow vs reject).
    """
    return [
        {"optionId": PERM_ALLOW_ONCE, "name": "Allow", "kind": PERM_ALLOW_ONCE},
        {"optionId": PERM_ALLOW_ALWAYS, "name": "Allow always", "kind": PERM_ALLOW_ALWAYS},
        {"optionId": PERM_REJECT_ONCE, "name": "Reject", "kind": PERM_REJECT_ONCE},
        {"optionId": PERM_REJECT_ALWAYS, "name": "Reject always", "kind": PERM_REJECT_ALWAYS},
    ]


def tool_call_update_for_permission(*, tool_call_id: str, tool_name: str = "", title: str = "") -> WireObject:
    """The ``ToolCallUpdate`` a ``session/request_permission`` request carries.

    ACP's request embeds the tool call awaiting approval as a ``toolCall``
    (ToolCallUpdate shape: ``toolCallId`` + optional overrides). We surface the
    kind + title so the editor renders *which* call it is gating.
    """
    update: WireObject = {"toolCallId": tool_call_id, "status": STATUS_PENDING}
    if tool_name:
        update["kind"] = tool_kind_for(tool_name)
    if title:
        update["title"] = title
    return update


def to_acp_updates(event: ev.ViewEvent, state: AcpWireState) -> list[WireObject]:
    """Map one ``ViewEvent`` to zero-or-more ACP ``update`` dicts.

    Pure w.r.t. ``(event, state)``: the only mutation is ``state``'s message-id /
    seen-tool correlation (unavoidable — ACP needs stable per-block ids + a
    tool_call-vs-tool_call_update distinction that mote's flat events don't
    carry). Unknown / display-only kinds → ``[]``.
    """
    disposition = adapter_disposition(event, frozenset(_DISPATCH))
    if disposition is ViewEventDisposition.INTENTIONALLY_OMITTED:
        return []
    handler = _DISPATCH[event.kind]
    return handler(event, state)


# ── per-kind handlers ───────────────────────────────────────────────────────
def _on_message_started(e: ev.MessageBlockStarted, st: AcpWireState) -> list[WireObject]:
    # Open a message id; ACP has no explicit "start" frame — the first chunk
    # carries the id. Nothing is emitted until content arrives.
    st.open_message()
    return []


def _on_message_delta(e: ev.MessageBlockDelta, st: AcpWireState) -> list[WireObject]:
    if not e.text:
        return []
    return [agent_text(e.text, st.current_message())]


def _on_message_completed(e: ev.MessageBlockCompleted, st: AcpWireState) -> list[WireObject]:
    # If the block streamed, the deltas already carried the text; just close the
    # id. If it never streamed, emit the whole markdown as one chunk.
    if e.streamed:
        st.close_message()
        return []
    mid = st.current_message()
    st.close_message()
    if not e.markdown:
        return []
    return [agent_text(e.markdown, mid)]


def _on_reasoning_delta(e: ev.ReasoningDelta, st: AcpWireState) -> list[WireObject]:
    if not e.text:
        return []
    return [_chunk(AGENT_THOUGHT_CHUNK, text_block(e.text))]


def _on_tool_started(e: ev.ToolCallStarted, st: AcpWireState) -> list[WireObject]:
    tool_call_id = _tool_id(e)
    st.seen_tools.add(tool_call_id)
    call: WireObject = {
        "sessionUpdate": TOOL_CALL,
        "toolCallId": tool_call_id,
        "title": e.title or e.tool_name or "tool",
        "kind": tool_kind_for(e.tool_name),
        "status": STATUS_IN_PROGRESS,
    }
    # The projector already picked the headline/body; surface it as a content
    # block so the editor can show the call before it completes.
    preview = e.headline or e.body
    if preview:
        call["content"] = [_content_block(preview)]
    return [call]


def _on_tool_completed(e: ev.ToolCallCompleted, st: AcpWireState) -> list[WireObject]:
    tool_call_id = _tool_id(e)
    update: WireObject = {
        "sessionUpdate": TOOL_CALL_UPDATE,
        "toolCallId": tool_call_id,
        "status": STATUS_COMPLETED if e.ok else STATUS_FAILED,
    }
    # If the start was never announced (non-streaming upstream), promote this to
    # a full tool_call so the editor still learns the kind/title.
    if tool_call_id not in st.seen_tools:
        _promote_to_call(update, st, title=e.tool_name or "tool", kind=tool_kind_for(e.tool_name))
    content = e.summary if e.ok else (e.recovery or e.error_code or e.summary or "error")
    if e.detail:
        content = f"{content}\n{e.detail}" if content else e.detail
    if content:
        update["content"] = [_content_block(content)]
    return [update]


def _on_file_diff(e: ev.FileDiffBlock, st: AcpWireState) -> list[WireObject]:
    # A file change → a tool_call_update carrying a ``diff`` content block, keyed
    # to the same toolCallId the Edit/Write tool call used. ``oldText`` is null
    # for a creation (ACP convention); ``newText`` empty for a deletion.
    tool_call_id = _tool_id(e)
    diff: WireObject = {
        "type": BLOCK_DIFF,
        "path": e.path,
        "oldText": e.old if e.old != "" else None,
        "newText": e.new,
    }
    update: WireObject = {
        "sessionUpdate": TOOL_CALL_UPDATE if tool_call_id in st.seen_tools else TOOL_CALL,
        "toolCallId": tool_call_id,
        "content": [diff],
    }
    if tool_call_id not in st.seen_tools:
        # A diff arriving before its tool_call start — announce a minimal edit call.
        _promote_to_call(update, st, title=e.path or "edit", kind=TOOL_KIND_EDIT, status=STATUS_IN_PROGRESS)
    # Locations let the editor jump to the changed file.
    if e.path:
        update["locations"] = [{"path": e.path}]
    return [update]


def _on_activity_started(e: ev.ActivityStarted, st: AcpWireState) -> list[WireObject]:
    label = e.label or e.activity_kind or "activity"
    return [agent_text(f"▶ {label}")]


def _on_activity_completed(e: ev.ActivityCompleted, st: AcpWireState) -> list[WireObject]:
    label = e.summary or e.outcome or "activity"
    return [agent_text(f"✓ {label}" if e.outcome == "success" else f"✗ {label}")]


def _on_notice(e: ev.Notice, st: AcpWireState) -> list[WireObject]:
    if not e.text:
        return []
    return [agent_text(e.text)]


def _on_error(e: ev.ErrorRaised, st: AcpWireState) -> list[WireObject]:
    if not e.text:
        return []
    return [agent_text(f"⚠ {e.text}")]


def _on_media(e: ev.MediaBlock, st: AcpWireState) -> list[WireObject]:
    # Degrade to a text pointer (an image ContentBlock needs base64 data we don't
    # carry inline here); the editor gets the alt text + locator.
    label = e.alt or e.ref or e.media_kind
    return [agent_text(f"[{e.media_kind}] {label}")]


def _on_async_work(e: ev.AsyncWorkObserved, st: AcpWireState) -> list[WireObject]:
    observation = decode_async_work_observation(json.loads(e.observation_json))
    return [
        {
            "sessionUpdate": "mote_async_work_observation",
            "observation": to_wire_json(encode_async_work_observation(observation)),
        }
    ]


# kind string → handler. Absent kinds (usage_updated, task_progress,
# question_asked, approval_requested, retry_status, conversation_compacted,
# transcript_cleared, session_list_shown, system_reminder) fall through to
# ``[]`` — display-only / handled out-of-band (approval rides the port's
# session/request_permission request, not a session/update notification).
_DISPATCH = {
    ev.MESSAGE_BLOCK_STARTED: _on_message_started,
    ev.MESSAGE_BLOCK_DELTA: _on_message_delta,
    ev.MESSAGE_BLOCK_COMPLETED: _on_message_completed,
    ev.REASONING_DELTA: _on_reasoning_delta,
    ev.TOOL_CALL_STARTED: _on_tool_started,
    ev.TOOL_CALL_COMPLETED: _on_tool_completed,
    ev.FILE_DIFF_BLOCK: _on_file_diff,
    ev.ACTIVITY_STARTED: _on_activity_started,
    ev.ACTIVITY_COMPLETED: _on_activity_completed,
    ev.NOTICE: _on_notice,
    ev.ERROR_RAISED: _on_error,
    ev.MEDIA_BLOCK: _on_media,
    ev.OUTPUT_SNAPSHOT: _on_output_snapshot,
    ev.OUTPUT_SNAPSHOT_INVALIDATED: _on_output_snapshot_invalidated,
    ev.OUTPUT_COMMITTED: _on_output_committed,
    ev.ASYNC_WORK_OBSERVED: _on_async_work,
}


__all__ = [
    "AcpWireState",
    "to_acp_updates",
    "text_block",
    "agent_text",
    "tool_kind_for",
    "permission_options",
    "tool_call_update_for_permission",
    "PERM_KIND_TO_OUTCOME",
    "PERM_ALLOW_ONCE",
    "PERM_ALLOW_ALWAYS",
    "PERM_REJECT_ONCE",
    "PERM_REJECT_ALWAYS",
    "USER_MESSAGE_CHUNK",
    "AGENT_MESSAGE_CHUNK",
    "AGENT_THOUGHT_CHUNK",
    "TOOL_CALL",
    "TOOL_CALL_UPDATE",
    "PLAN",
    "STATUS_PENDING",
    "STATUS_IN_PROGRESS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
]
