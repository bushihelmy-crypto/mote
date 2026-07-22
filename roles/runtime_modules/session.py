"""Session-domain component manifest and event subscribers.

This module owns every Role component whose durable source of truth is the
session workspace.  The composition root consumes the manifest; it does not
need to know how individual recorders are constructed.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from mote.roles.component_graph import ComponentSpec
from mote.router.router import COMPRESSION_TASK
from mote.session import (
    BrowserStateRecorder,
    FileSnapshotRecorder,
    HunkLedger,
    KernelStateRecorder,
    SessionLog,
    TerminalStateRecorder,
)
from mote.session.snapshot import detect_blob_backend
from mote.session.subscribers import CheckpointSubscriber, HunkSubscriber, RecorderSubscriber, TitleSubscriber


def session_component_specs() -> list[ComponentSpec]:
    """Return the complete session-owned portion of the Role component graph."""
    return [
        ComponentSpec("session_log", _build_session_log),
        ComponentSpec("file_snapshot_recorder", _build_file_snapshot_recorder),
        ComponentSpec("hunk_ledger", _build_hunk_ledger),
        ComponentSpec("hunk_subscriber", _build_hunk_subscriber),
        ComponentSpec("checkpoint_subscriber", _build_checkpoint_recorder),
        ComponentSpec("title_subscriber", _build_title_subscriber),
        ComponentSpec("terminal_state_recorder", _build_terminal_state_recorder),
        ComponentSpec("kernel_state_recorder", _build_kernel_state_recorder),
        ComponentSpec("browser_state_recorder", _build_browser_state_recorder),
    ]


def session_event_subscribers(get: Callable[[str], Any]) -> list:
    """Build the session-owned slice of the event-bus subscriber roster."""
    return [
        RecorderSubscriber(get("session_log")),
        get("checkpoint_subscriber"),
        get("title_subscriber"),
        get("hunk_subscriber"),
    ]


def _build_session_log(ctx) -> SessionLog:
    # Construction stays I/O-free. Role._emit_session_start creates the meta
    # record at the explicit startup boundary before any event is appended.
    return SessionLog(ctx.role.state.session_id)


def _build_file_snapshot_recorder(ctx) -> FileSnapshotRecorder:
    backend = ctx.role.role_schema.snapshot_backend
    if backend == "auto":
        backend = detect_blob_backend(ctx.role.state.working_dir or None)
    return FileSnapshotRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_file_history,
        backend=backend,
    )


def _build_hunk_ledger(ctx) -> HunkLedger:
    return HunkLedger(ctx.role.state.session_id, store=ctx.dep("workspace_store"))


def _build_hunk_subscriber(ctx) -> Optional[HunkSubscriber]:
    role = ctx.role
    if not role.role_schema.record_hunks:
        return None
    return HunkSubscriber(
        ctx.dep("hunk_ledger"),
        role.current_turn_index,
        role.state.session_id,
        ctx.dep("file_snapshot_recorder").blobs,
        enabled=True,
    )


def _build_checkpoint_recorder(ctx) -> Optional[CheckpointSubscriber]:
    role = ctx.role
    if not role.role_schema.record_checkpoints:
        return None
    if detect_blob_backend(role.state.working_dir or None) != "git":
        return None
    return CheckpointSubscriber(
        ctx.dep("session_log"),
        lambda: role.state.working_dir,
        enabled=True,
    )


_TITLE_SYSTEM_PROMPT = (
    "You name a chat session from its opening message. Reply with ONLY a short "
    "title (at most 6 words, no quotes, no trailing punctuation) capturing the "
    "user's intent. No preamble, no explanation — just the title."
)
_TITLE_MAX_LEN = 80


def _build_title_subscriber(ctx) -> Optional[TitleSubscriber]:
    role = ctx.role
    if not role.role_schema.generate_title:
        return None

    async def _generate(prompt: str) -> Optional[str]:
        llm = ctx.dep("router").route_for_task(COMPRESSION_TASK)
        title = await llm.aask(prompt, system_msgs=[_TITLE_SYSTEM_PROMPT], stream=False)
        return (title or "").strip().strip('"').strip()[:_TITLE_MAX_LEN] or None

    return TitleSubscriber(ctx.dep("session_log"), _generate, enabled=True)


def _build_terminal_state_recorder(ctx) -> TerminalStateRecorder:
    return TerminalStateRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_terminal_state,
    )


def _build_kernel_state_recorder(ctx) -> KernelStateRecorder:
    return KernelStateRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_kernel_state,
    )


def _build_browser_state_recorder(ctx) -> BrowserStateRecorder:
    return BrowserStateRecorder(
        ctx.dep("session_log"),
        enabled=ctx.role.role_schema.record_browser_state,
    )
