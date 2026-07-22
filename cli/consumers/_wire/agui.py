#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``ViewEvent → AG-UI event`` — the pure mapper (Phase 1 core asset).

AG-UI (https://docs.ag-ui.com) is the open agent-UI event protocol CopilotKit's
v2 runtime speaks over SSE (single-line JSON per ``data:`` frame). This module is
the **transport-free** translation table from mote's neutral ``ViewEvent`` spine
to AG-UI event dicts — no sockets, no SSE, no CopilotKit private API. The AG-UI
consumer (``consumers/agui/``) owns the HTTP/SSE transport and calls
:func:`to_agui_events` to serialize; every wire-shape decision lives *here* so it
is unit-testable in isolation and swappable without touching transport.

Design notes
------------
* **Fan-out, not 1:1.** One ViewEvent can map to several AG-UI events (a tool
  start → ``TOOL_CALL_START`` + ``TOOL_CALL_ARGS``), so the mapper returns a
  ``list[dict]`` (possibly empty). An unknown / display-only kind → ``[]`` (a
  forward-compatible frontend simply never sees it — never an error).
* **Id correlation.** AG-UI keys streaming text by ``messageId`` and tool calls
  by ``toolCallId``. mote already carries ``tool_use_id`` on tool events; for
  message blocks (which have no per-block id on the wire until completion) the
  mapper mints a stable per-run ``messageId`` from a monotonic block counter.
  :class:`AguiWireState` holds that tiny per-run correlation state so the mapper
  functions themselves stay pure w.r.t. their inputs.
* **AG-UI wire vocabulary** (verified against CopilotKit v2 runtime fixtures):
  ``RUN_STARTED{threadId,runId}``, ``TEXT_MESSAGE_START{messageId,role}``,
  ``TEXT_MESSAGE_CONTENT{messageId,delta}``, ``TEXT_MESSAGE_END{messageId}``,
  ``TOOL_CALL_START{toolCallId,toolCallName,parentMessageId?}``,
  ``TOOL_CALL_ARGS{toolCallId,delta}``, ``TOOL_CALL_END{toolCallId}``,
  ``TOOL_CALL_RESULT{toolCallId,messageId,content}``,
  ``STEP_STARTED{stepName}``, ``STEP_FINISHED{stepName}``,
  ``STATE_SNAPSHOT{snapshot}``, ``RUN_FINISHED{threadId,runId}``,
  ``RUN_ERROR{message,code?}``, ``CUSTOM{name,value}`` (reasoning / approval /
  question / notices ride ``CUSTOM`` so no bespoke frontend handler is required).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mote.cli.contracts.view import events as ev

# ── AG-UI event type strings (the wire ``type`` discriminator) ──────────────
RUN_STARTED = "RUN_STARTED"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"
TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
TOOL_CALL_START = "TOOL_CALL_START"
TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
TOOL_CALL_END = "TOOL_CALL_END"
TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
STEP_STARTED = "STEP_STARTED"
STEP_FINISHED = "STEP_FINISHED"
STATE_SNAPSHOT = "STATE_SNAPSHOT"
CUSTOM = "CUSTOM"


@dataclass
class AguiWireState:
    """Per-run correlation state (message/tool ids) for one AG-UI stream.

    One instance per ``POST /run`` (one turn). Holds the monotonic block counter
    used to mint a stable ``messageId`` for an assistant message block (mote
    message blocks carry no id on the wire until completion) and remembers the
    currently-open block id so ``delta``/``end`` events reference the same one.
    Tool calls need no state here — they carry ``tool_use_id`` end-to-end.
    """

    thread_id: str
    run_id: str
    _block_seq: int = field(default=0)
    _open_message_id: Optional[str] = field(default=None)

    def open_message(self) -> str:
        """Mint + remember a fresh ``messageId`` for a newly-opened block."""
        self._block_seq += 1
        self._open_message_id = f"{self.run_id}-msg-{self._block_seq}"
        return self._open_message_id

    def current_message(self) -> str:
        """The open block's id (mint one lazily if a delta/complete arrives first)."""
        if self._open_message_id is None:
            return self.open_message()
        return self._open_message_id

    def close_message(self) -> str:
        """Return the open block's id and clear it (block boundary)."""
        mid = self.current_message()
        self._open_message_id = None
        return mid


# ── Run lifecycle (emitted by the transport, not folded from a ViewEvent) ───
def run_started(state: AguiWireState) -> Dict[str, Any]:
    """The ``RUN_STARTED`` frame the transport emits before streaming a turn."""
    return {"type": RUN_STARTED, "threadId": state.thread_id, "runId": state.run_id}


def run_finished(state: AguiWireState) -> Dict[str, Any]:
    """The ``RUN_FINISHED`` frame the transport emits after a turn's events."""
    return {"type": RUN_FINISHED, "threadId": state.thread_id, "runId": state.run_id}


def _custom(name: str, value: Any) -> Dict[str, Any]:
    """A ``CUSTOM`` event — the escape hatch for kinds without a native AG-UI
    shape (reasoning / approval / question / notice / compaction / retry). A
    frontend renders known ``name``s and ignores the rest; nothing breaks."""
    return {"type": CUSTOM, "name": name, "value": value}


# ── HITL prompt frames (Phase 3) ────────────────────────────────────────────
# Emitted by the *port* (not folded from a ViewEvent): a gated tool call blocks
# the turn on the human, so the port pushes one of these down THIS run's SSE
# stream and awaits a back-channel ``/respond`` keyed by the minted id. Kept
# beside the wire shapes so the frame vocabulary lives in one place; the
# ``_on_approval`` / ``_on_question`` ViewEvent handlers delegate here too.
def approval_prompt(
    *,
    approval_id: str,
    tool_name: str = "",
    action: str = "",
    args_preview: str = "",
    risk: str = "medium",
) -> Dict[str, Any]:
    """The ``CUSTOM{name:'approval'}`` frame the frontend answers via ``/respond``.

    ``approvalId`` correlates the frame to the ``/respond`` body that resolves
    it; the frontend renders ``action`` / ``argsPreview`` / ``risk`` and posts
    back ``{promptId: approvalId, outcome, editedArgs?}``.
    """
    return _custom(
        "approval",
        {
            "approvalId": approval_id,
            "toolName": tool_name,
            "action": action,
            "argsPreview": args_preview,
            "risk": risk,
        },
    )


def question_prompt(
    *,
    question_id: str = "",
    question: str = "",
    options: Optional[List[str]] = None,
    structured: Optional[Any] = None,
) -> Dict[str, Any]:
    """The ``CUSTOM{name:'question'}`` frame for a free-text or structured ask.

    ``questionId`` correlates the answer posted to ``/respond``; ``options`` is
    the flat label list for a simple pick; ``structured`` (when set) carries the
    full multi-question payload so a rich frontend can render selects, and the
    answer comes back as ``{promptId, answers:[...]}``.
    """
    value: Dict[str, Any] = {"questionId": question_id, "question": question, "options": options or []}
    if structured is not None:
        value["structured"] = structured
    return _custom("question", value)


def to_agui_events(event: ev.ViewEvent, state: AguiWireState) -> List[Dict[str, Any]]:
    """Map one ``ViewEvent`` to zero-or-more AG-UI event dicts.

    Pure w.r.t. ``(event, state)``: the only mutation is ``state``'s message-id
    correlation counters (unavoidable — AG-UI needs stable per-block ids that
    mote's block deltas don't carry). Unknown / display-only kinds → ``[]``.
    """
    kind = getattr(event, "kind", None)
    handler = _DISPATCH.get(kind) if kind else None
    if handler is None:
        return []
    return handler(event, state)


# ── per-kind handlers ───────────────────────────────────────────────────────
def _on_message_started(e: ev.MessageBlockStarted, st: AguiWireState) -> List[Dict[str, Any]]:
    return [{"type": TEXT_MESSAGE_START, "messageId": st.open_message(), "role": e.role}]


def _on_message_delta(e: ev.MessageBlockDelta, st: AguiWireState) -> List[Dict[str, Any]]:
    return [{"type": TEXT_MESSAGE_CONTENT, "messageId": st.current_message(), "delta": e.text}]


def _on_message_completed(e: ev.MessageBlockCompleted, st: AguiWireState) -> List[Dict[str, Any]]:
    # If the block never streamed (non-streaming upstream), synthesize the whole
    # start→content→end triple so the frontend still gets a complete message.
    if e.streamed:
        return [{"type": TEXT_MESSAGE_END, "messageId": st.close_message()}]
    mid = st.open_message()
    st.close_message()
    return [
        {"type": TEXT_MESSAGE_START, "messageId": mid, "role": e.role},
        {"type": TEXT_MESSAGE_CONTENT, "messageId": mid, "delta": e.markdown},
        {"type": TEXT_MESSAGE_END, "messageId": mid},
    ]


def _on_reasoning_delta(e: ev.ReasoningDelta, st: AguiWireState) -> List[Dict[str, Any]]:
    # Reasoning has no first-class AG-UI text channel here; ride CUSTOM so a
    # frontend can render a "thinking" stream without a bespoke handler.
    return [_custom("reasoning", {"delta": e.text})]


def _on_tool_started(e: ev.ToolCallStarted, st: AguiWireState) -> List[Dict[str, Any]]:
    tool_call_id = e.tool_use_id or f"{st.run_id}-tool-{id(e)}"
    out: List[Dict[str, Any]] = [{"type": TOOL_CALL_START, "toolCallId": tool_call_id, "toolCallName": e.tool_name}]
    # The projector already picked the headline/body; pass them as the tool's
    # arg preview so the frontend can render the call before it completes.
    args_preview = e.headline or e.body
    if args_preview:
        out.append({"type": TOOL_CALL_ARGS, "toolCallId": tool_call_id, "delta": args_preview})
    return out


def _on_tool_completed(e: ev.ToolCallCompleted, st: AguiWireState) -> List[Dict[str, Any]]:
    tool_call_id = e.tool_use_id or f"{st.run_id}-tool-{id(e)}"
    content = e.summary if e.ok else (e.recovery or e.error_code or e.summary or "error")
    if e.detail:
        content = f"{content}\n{e.detail}" if content else e.detail
    return [
        {"type": TOOL_CALL_END, "toolCallId": tool_call_id},
        {
            "type": TOOL_CALL_RESULT,
            "toolCallId": tool_call_id,
            "messageId": f"{tool_call_id}-result",
            "content": content,
        },
    ]


def _on_task_progress(e: ev.TaskProgress, st: AguiWireState) -> List[Dict[str, Any]]:
    label = e.stage or e.status or "task"
    return [{"type": STEP_STARTED, "stepName": label}]


def _on_activity_started(e: ev.ActivityStarted, st: AguiWireState) -> List[Dict[str, Any]]:
    return [{"type": STEP_STARTED, "stepName": e.label or e.activity_kind or "activity"}]


def _on_activity_completed(e: ev.ActivityCompleted, st: AguiWireState) -> List[Dict[str, Any]]:
    return [{"type": STEP_FINISHED, "stepName": e.summary or e.outcome or "activity"}]


def _on_usage(e: ev.UsageUpdated, st: AguiWireState) -> List[Dict[str, Any]]:
    snapshot = {
        "usage": {
            "inputTokens": e.input_tokens,
            "outputTokens": e.output_tokens,
            "totalTokens": e.total_tokens,
            "costUsd": e.cost_usd,
            "contextPct": e.context_pct,
            "model": e.model,
        }
    }
    return [{"type": STATE_SNAPSHOT, "snapshot": snapshot}]


def _on_approval(e: ev.ApprovalRequested, st: AguiWireState) -> List[Dict[str, Any]]:
    # Display-side echo of an approval (if a projector ever emits one); the live
    # HITL round-trip goes through the port, which builds the same frame.
    return [
        approval_prompt(
            approval_id=e.approval_id,
            tool_name=e.tool_name,
            action=e.action,
            args_preview=e.args_preview,
            risk=e.risk,
        )
    ]


def _on_question(e: ev.QuestionAsked, st: AguiWireState) -> List[Dict[str, Any]]:
    return [question_prompt(question=e.question, options=e.options)]


def _on_error(e: ev.ErrorRaised, st: AguiWireState) -> List[Dict[str, Any]]:
    return [{"type": RUN_ERROR, "message": e.text}]


def _on_notice(e: ev.Notice, st: AguiWireState) -> List[Dict[str, Any]]:
    return [_custom("notice", {"text": e.text, "level": e.level})]


def _on_media(e: ev.MediaBlock, st: AguiWireState) -> List[Dict[str, Any]]:
    return [
        _custom(
            "media",
            {"mediaKind": e.media_kind, "ref": e.ref, "mime": e.mime, "alt": e.alt},
        )
    ]


def _on_file_diff(e: ev.FileDiffBlock, st: AguiWireState) -> List[Dict[str, Any]]:
    return [_custom("fileDiff", {"path": e.path, "old": e.old, "new": e.new})]


def _on_compacted(e: ev.ConversationCompacted, st: AguiWireState) -> List[Dict[str, Any]]:
    return [_custom("compacted", {"summary": e.summary, "messageCount": e.message_count})]


def _on_retry(e: ev.RetryStatus, st: AguiWireState) -> List[Dict[str, Any]]:
    return [
        _custom(
            "retry",
            {"attempt": e.attempt, "maxAttempts": e.max_attempts, "delayMs": e.delay_ms},
        )
    ]


# kind string → handler. Absent kinds (transcript_cleared, session_list_shown,
# system_reminder, ...) fall through to ``[]`` — display-only on a fresh SSE run.
_DISPATCH = {
    ev.MESSAGE_BLOCK_STARTED: _on_message_started,
    ev.MESSAGE_BLOCK_DELTA: _on_message_delta,
    ev.MESSAGE_BLOCK_COMPLETED: _on_message_completed,
    ev.REASONING_DELTA: _on_reasoning_delta,
    ev.TOOL_CALL_STARTED: _on_tool_started,
    ev.TOOL_CALL_COMPLETED: _on_tool_completed,
    ev.TASK_PROGRESS: _on_task_progress,
    ev.ACTIVITY_STARTED: _on_activity_started,
    ev.ACTIVITY_COMPLETED: _on_activity_completed,
    ev.USAGE_UPDATED: _on_usage,
    ev.APPROVAL_REQUESTED: _on_approval,
    ev.QUESTION_ASKED: _on_question,
    ev.ERROR_RAISED: _on_error,
    ev.NOTICE: _on_notice,
    ev.MEDIA_BLOCK: _on_media,
    ev.FILE_DIFF_BLOCK: _on_file_diff,
    ev.CONVERSATION_COMPACTED: _on_compacted,
    ev.RETRY_STATUS: _on_retry,
}


def encode_sse(event: Dict[str, Any]) -> str:
    """Serialize one AG-UI event dict as an SSE ``data:`` frame (single-line JSON).

    AG-UI frames are ``data: <json>\\n\\n`` with the JSON on ONE line (no
    embedded newlines). Kept here (beside the shapes) so the transport just
    writes the returned bytes.
    """
    return "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"


__all__ = [
    "AguiWireState",
    "to_agui_events",
    "run_started",
    "run_finished",
    "approval_prompt",
    "question_prompt",
    "encode_sse",
    "RUN_STARTED",
    "RUN_FINISHED",
    "RUN_ERROR",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "TOOL_CALL_START",
    "TOOL_CALL_ARGS",
    "TOOL_CALL_END",
    "TOOL_CALL_RESULT",
    "STEP_STARTED",
    "STEP_FINISHED",
    "STATE_SNAPSHOT",
    "CUSTOM",
]
