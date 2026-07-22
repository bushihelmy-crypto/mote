"""Unified per-turn ephemeral context injection layer.

One bus aggregating pluggable feeds of request-only context (git status,
token-pressure notes, background-task progress, LSP diagnostics, ...). Each feed
is an :class:`~mote.common.interface.EphemeralContextSource`; the bus renders
them per think() cycle and merges the non-empty blocks into a single
``<system-reminder>`` appended to the user prompt — never stored in history.

Lives in the low ``context`` layer: depends only on ``common`` + injected
sources. Sources requiring higher layers (``tasks``) live there and are wired in
by ``Role``.
"""

from mote.context.turn_context.bus import TurnContextBus
from mote.context.turn_context.format import wrap_system_reminder
from mote.context.turn_context.sources import (
    ChangedFilesContextSource,
    CodeMapContextSource,
    CompactionNoticeContextSource,
    CredentialIndexContextSource,
    DeferredToolIndexContextSource,
    FoldPressureContextSource,
    GitContextSource,
    SkillActivationContextSource,
    SkillListingContextSource,
    SplitToolMenuContextSource,
    TeamContextSource,
    TimestampContextSource,
    TokenPressureContextSource,
    ToolCatalogContextSource,
)

__all__ = [
    "TurnContextBus",
    "wrap_system_reminder",
    "ChangedFilesContextSource",
    "CodeMapContextSource",
    "CompactionNoticeContextSource",
    "CredentialIndexContextSource",
    "DeferredToolIndexContextSource",
    "FoldPressureContextSource",
    "GitContextSource",
    "SkillActivationContextSource",
    "SkillListingContextSource",
    "SplitToolMenuContextSource",
    "TeamContextSource",
    "TimestampContextSource",
    "TokenPressureContextSource",
    "ToolCatalogContextSource",
]
