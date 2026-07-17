#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``RenderSurface`` — the per-host landing pad for the neutral op stream.

One method per :class:`~mote.cli.consumers.transcript.ops.TranscriptOp`, named
exactly like the op's ``kind`` so the
:class:`~mote.cli.consumers.transcript.driver.SurfaceDriver` dispatches with a
single ``getattr``. Each host implements a surface that lands an op with its own
primitives — a scrolling terminal prints a line / opens a transient ``Live``; a
Textual app mounts / mutates a widget — while the reducer that feeds them stays
completely host-blind.

:class:`BaseSurface` defaults every method to a no-op (mirroring
``BaseConsumer.on_unhandled`` eating an unknown kind), so a host overrides only
the ops it renders and a newly added op never breaks an existing surface.

Interaction (ctrl+o fold toggles, click-to-select, mouse copy) produces **no
op** — it is an after-the-fact repaint of already-shown rows, which only a host
that retains its DOM can do — so it stays inside that host, driven by capability
rather than a type sniff.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mote.cli.consumers.render.builders import FoldMode
from mote.cli.consumers.transcript.ops import Truncation


@runtime_checkable
class RenderSurface(Protocol):
    """Structural contract: one method per op ``kind`` (+ ``close``)."""

    # block lifecycle
    def open_block(self, role: str) -> None:
        ...

    def append_delta(self, text: str, reasoning: bool) -> None:
        ...

    def close_block(self, markdown: str, streamed: bool, truncation: Truncation) -> None:
        ...

    def render_user_message(self, markdown: str) -> None:
        ...

    # standalone tools
    def tool_started(self, ev: Any, fold: FoldMode) -> None:
        ...

    def tool_completed(self, ev: Any, fold: FoldMode, truncation: Truncation) -> None:
        ...

    # collapsed group
    def open_group(self) -> None:
        ...

    def add_to_group(self, ev: Any) -> None:
        ...

    def complete_in_group(self, ev: Any) -> None:
        ...

    def flush_group(self) -> None:
        ...

    # nested activity (a run_graph orchestration; a sub-agent / bg task)
    def open_activity(self, scope: Any, activity_kind: str, label: str, topology: Any) -> None:
        ...

    def update_activity_node(self, scope: Any, stage: str, status: str, detail: str) -> None:
        ...

    def add_activity_tool_call(self, scope: Any, ev: Any) -> None:
        ...

    def complete_activity_tool_call(self, scope: Any, ev: Any) -> None:
        ...

    def close_activity(self, scope: Any, outcome: str, node_states: Any, summary: str) -> None:
        ...

    # static rows
    def render_media(self, ev: Any) -> None:
        ...

    def render_file_diff(self, ev: Any) -> None:
        ...

    def render_task_progress(self, ev: Any) -> None:
        ...

    def render_notice(self, ev: Any) -> None:
        ...

    def render_system_reminder(self, ev: Any) -> None:
        ...

    def render_error(self, ev: Any) -> None:
        ...

    def render_question(self, ev: Any) -> None:
        ...

    def render_approval(self, ev: Any) -> None:
        ...

    def render_session_list(self, ev: Any) -> None:
        ...

    # transient chrome
    def set_thinking(self, on: bool) -> None:
        ...

    def set_retry(self, ev: Any) -> None:
        ...

    def clear_retry(self) -> None:
        ...

    def update_usage(self, ev: Any) -> None:
        ...

    # boundaries / destructive
    def clear_for_compaction(self, summary: str, message_count: int, last_user_prompt: str) -> None:
        ...

    def clear_transcript(self) -> None:
        ...

    # teardown
    def close(self) -> None:
        ...


class BaseSurface:
    """No-op defaults for every op; a host overrides only what it renders."""

    def open_block(self, role: str) -> None:
        return None

    def append_delta(self, text: str, reasoning: bool) -> None:
        return None

    def close_block(self, markdown: str, streamed: bool, truncation: Truncation) -> None:
        return None

    def render_user_message(self, markdown: str) -> None:
        return None

    def tool_started(self, ev: Any, fold: FoldMode) -> None:
        return None

    def tool_completed(self, ev: Any, fold: FoldMode, truncation: Truncation) -> None:
        return None

    def open_group(self) -> None:
        return None

    def add_to_group(self, ev: Any) -> None:
        return None

    def complete_in_group(self, ev: Any) -> None:
        return None

    def flush_group(self) -> None:
        return None

    def open_activity(self, scope: Any, activity_kind: str, label: str, topology: Any) -> None:
        return None

    def update_activity_node(self, scope: Any, stage: str, status: str, detail: str) -> None:
        return None

    def add_activity_tool_call(self, scope: Any, ev: Any) -> None:
        return None

    def complete_activity_tool_call(self, scope: Any, ev: Any) -> None:
        return None

    def close_activity(self, scope: Any, outcome: str, node_states: Any, summary: str) -> None:
        return None

    def render_media(self, ev: Any) -> None:
        return None

    def render_file_diff(self, ev: Any) -> None:
        return None

    def render_task_progress(self, ev: Any) -> None:
        return None

    def render_notice(self, ev: Any) -> None:
        return None

    def render_system_reminder(self, ev: Any) -> None:
        return None

    def render_error(self, ev: Any) -> None:
        return None

    def render_question(self, ev: Any) -> None:
        return None

    def render_approval(self, ev: Any) -> None:
        return None

    def render_session_list(self, ev: Any) -> None:
        return None

    def set_thinking(self, on: bool) -> None:
        return None

    def set_retry(self, ev: Any) -> None:
        return None

    def clear_retry(self) -> None:
        return None

    def update_usage(self, ev: Any) -> None:
        return None

    def clear_for_compaction(self, summary: str, message_count: int, last_user_prompt: str) -> None:
        return None

    def clear_transcript(self) -> None:
        return None

    def close(self) -> None:
        return None


__all__ = ["RenderSurface", "BaseSurface"]
