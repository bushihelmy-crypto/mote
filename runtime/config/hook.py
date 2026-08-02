"""Runtime hook configuration.

Lives in Runtime configuration; Product composition projects it into ``RoleSchema``
(which declares it) can reference it without importing the hook engine. The
engine itself lives in ``mote.runtime.hook``; this is only the declarative
shape: per-event lists of matcher groups.

Default: a Role with ``hooks=None`` (the default) runs with no hook layer. Python callbacks are NOT declared here — they are registered
programmatically on the ``HookManager`` (the SDK-style path).
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import ConfigDict, Field

from mote.contracts.config.base import ConfigModel


class HookCommandHandler(ConfigModel):
    """One external command handler (JSON stdin/stdout contract).

    The command receives the hook input as a JSON line on stdin and may
    influence the host via its exit code (2 = block) and/or JSON on stdout.
    """

    type: Literal["command"] = "command"
    id: str = Field(min_length=1, description="Stable identity used by permission audit.")
    argv: tuple[str, ...] = Field(
        min_length=1,
        description="Executable and arguments. Shell parsing is never used.",
    )
    timeout: Optional[float] = Field(
        default=None,
        description="Per-handler timeout in seconds (falls back to the engine default).",
    )
    async_: bool = Field(
        default=False,
        alias="async",
        description="Fire-and-forget (reserved; not awaited for influence). Phase 1: ignored.",
    )
    status_message: str = Field(default="", description="Optional UI status text while running.")

    model_config = ConfigDict(populate_by_name=True)

    def model_post_init(self, __context: object) -> None:
        if not self.id.strip():
            raise ValueError("hook handler id must not be blank")
        if any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("hook argv must contain only non-empty strings")
        if not os.path.isabs(self.argv[0]):
            raise ValueError("hook executable must be an absolute path")


class HookMatcherGroup(ConfigModel):
    """A matcher + the handlers that run when it matches.

    ``matcher`` is matched against the event's match field (e.g. the tool name
    for PreToolUse): ``None``/``*`` = all, ``A|B`` = exact pipe list, else regex.
    Events without a match field always run their handlers.
    """

    matcher: Optional[str] = Field(default=None, description="Match pattern; None/'*' = all.")
    handlers: list[HookCommandHandler] = Field(default_factory=list)


class HookConfig(ConfigModel):
    """Per-Role hook policy, declared on :class:`RoleSchema`.

    ``events`` is keyed by event name::

        events = {
            "PreToolUse": [HookMatcherGroup(matcher="Bash", handlers=[...])],
            "Stop":       [HookMatcherGroup(handlers=[...])],
        }
    """

    events: dict[str, list[HookMatcherGroup]] = Field(
        default_factory=dict,
        description="Event name -> matcher groups (command handlers).",
    )

    def model_post_init(self, __context: object) -> None:
        handler_ids = [handler.id for groups in self.events.values() for group in groups for handler in group.handlers]
        if len(handler_ids) != len(set(handler_ids)):
            raise ValueError("hook handler ids must be unique")


__all__ = ["HookConfig", "HookCommandHandler", "HookMatcherGroup"]
