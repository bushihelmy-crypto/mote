"""Observation adapter from immutable facts to lifecycle hooks.

Policy hooks are intentionally absent: PreToolUse, UserPromptSubmit, PreCompact,
and Stop are invoked by their sealed domain policies. This subscriber only maps
facts that have already happened to advisory lifecycle notifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from mote.contracts.events.conversation import POST_COMPACT, PostCompactEvent
from mote.contracts.events.file.observation import FILE_CHANGED, FileChangedEvent
from mote.contracts.events.session import SESSION_START, SessionStartEvent
from mote.contracts.hook.invocation import FileChangedPayload

_E = TypeVar("_E")


@dataclass(frozen=True)
class _HookBinding(Generic[_E]):
    hook_name: str
    payload: Callable[[_E], dict]


_BINDINGS: dict[str, _HookBinding] = {
    POST_COMPACT: _HookBinding["PostCompactEvent"](
        "PostCompact",
        lambda event: {
            "trigger": event.trigger,
            "compact_summary": event.summary,
        },
    ),
    SESSION_START: _HookBinding["SessionStartEvent"](
        "SessionStart",
        lambda event: {"source": event.source},
    ),
}


class HookSubscriber:
    """Fire advisory lifecycle hooks for matching immutable facts."""

    def __init__(self, hook_manager) -> None:
        self._hook = hook_manager

    async def handle(self, event) -> None:
        if isinstance(event, FileChangedEvent):
            await self._hook.fire_file_changed(
                FileChangedPayload(
                    path=event.path,
                    change_type=event.change_type,
                    prior_version=event.prior_version,
                    version=event.version,
                    attribution=event.attribution,
                )
            )
            return
        binding = _BINDINGS.get(event.name)
        if binding is not None:
            await self._hook.fire(binding.hook_name, binding.payload(event))


__all__ = ["HookSubscriber"]
