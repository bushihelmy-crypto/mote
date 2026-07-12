"""HookSubscriber — adapts the existing HookManager onto the event bus.

The hook layer is preserved intact; this thin subscriber is the single seam that
feeds it. It translates the bus's *control* events into ``HookManager.fire``
calls and returns the folded :class:`HookOutcome` so the emitter (executor /
context manager / role) reads the same veto/mutate/stop influence it used to get
from a direct ``fire``.

It subscribes at a **low priority** so it runs before the recorder: a denied
tool call or vetoed compaction is folded before anything downstream persists it.

Observation events (stream deltas, message-appended, turn boundaries) are not
hooks — they return ``None`` here and are handled by other subscribers.

Not exported from ``common/hook/__init__`` on purpose: importing it pulls in
``common.events``, so it is imported lazily at the wiring site (Role) to keep the
hook package's import graph minimal.
"""

from __future__ import annotations

from typing import Optional

from metagpt.common.events.types import (
    FileChangedEvent,
    PostCompactEvent,
    PostToolUseEvent,
    PreCompactEvent,
    PreToolUseEvent,
    SessionStartEvent,
    TurnEndEvent,
    UserPromptSubmitEvent,
)
from metagpt.common.hook.types import HookOutcome


class HookSubscriber:
    """Routes control events to the wrapped :class:`HookManager`.

    Exposing ``handle_control`` (not ``handle``) is what places this subscriber on
    the bus's **control plane**: the bus awaits it inline, in priority order, and
    folds the :class:`HookOutcome` it returns into the value the emitter reads.
    This is the *only* subscriber that can veto/mutate/stop — influence over the
    host is confined to the control plane by construction.
    """

    #: Run early so a hook veto lands before the recorder persists the event.
    priority: int = 10

    def __init__(self, hook_manager) -> None:
        self._hook = hook_manager

    async def handle_control(self, event) -> Optional[HookOutcome]:
        name, payload = self._to_fire(event)
        if name is None:
            return None
        return await self._hook.fire(name, payload)

    @staticmethod
    def _to_fire(event):
        """Map a bus event to a ``(hook_event_name, payload)`` pair, or (None, {})."""
        if isinstance(event, UserPromptSubmitEvent):
            return "UserPromptSubmit", {"prompt": event.prompt}
        if isinstance(event, PreToolUseEvent):
            return "PreToolUse", {
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
                "tool_use_id": event.tool_use_id,
            }
        if isinstance(event, PostToolUseEvent):
            return "PostToolUse", {
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
                "tool_response": event.tool_response,
                "tool_use_id": event.tool_use_id,
            }
        if isinstance(event, PreCompactEvent):
            return "PreCompact", {"trigger": event.trigger}
        if isinstance(event, PostCompactEvent):
            return "PostCompact", {"trigger": event.trigger, "compact_summary": event.summary}
        if isinstance(event, SessionStartEvent):
            return "SessionStart", {"source": event.source}
        if isinstance(event, TurnEndEvent):
            # The legacy "Stop" hook fired at the turn boundary.
            return "Stop", {}
        if isinstance(event, FileChangedEvent):
            return "FileChanged", {
                "path": event.path,
                "change_type": event.change_type,
                "mtime": event.mtime,
                "size": event.size,
            }
        return None, {}


__all__ = ["HookSubscriber"]
