"""Runtime hook configuration.

Lives in ``common/schema`` alongside ``permission_config.py`` so ``RoleSchema``
(which declares it) can reference it without importing the hook engine. The
engine itself lives in ``mote.runtime.hook``; this is only the declarative
shape: per-event lists of matcher groups.

Default: a Role with ``hooks=None`` (the default) runs with no hook layer. Python callbacks are NOT declared here — they are registered
programmatically on the ``HookManager`` (the SDK-style path).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class HookCommandHandler(BaseModel):
    """One external command handler (JSON stdin/stdout contract).

    The command receives the hook input as a JSON line on stdin and may
    influence the host via its exit code (2 = block) and/or JSON on stdout.
    """

    type: Literal["command"] = "command"
    command: str = Field(description="Shell command to run for this hook.")
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

    model_config = {"populate_by_name": True}


class HookMatcherGroup(BaseModel):
    """A matcher + the handlers that run when it matches.

    ``matcher`` is matched against the event's match field (e.g. the tool name
    for PreToolUse): ``None``/``*`` = all, ``A|B`` = exact pipe list, else regex.
    Events without a match field always run their handlers.
    """

    matcher: Optional[str] = Field(default=None, description="Match pattern; None/'*' = all.")
    handlers: list[HookCommandHandler] = Field(default_factory=list)


class HookConfig(BaseModel):
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


__all__ = ["HookConfig", "HookCommandHandler", "HookMatcherGroup"]
