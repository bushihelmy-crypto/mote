"""Unified per-turn ephemeral context injection layer.

One bus aggregating pluggable feeds of request-only context (git status,
token-pressure notes, background-task progress, LSP diagnostics, ...). Each feed
is an :class:`~mote.contracts.ports.EphemeralContextSource`; the bus renders
them per think() cycle and merges the non-empty blocks into a single
``<system-reminder>`` appended to the user prompt — never stored in history.

Lives in the low ``context`` layer: depends only on ``common`` + injected
sources. Sources requiring higher layers (``tasks``) live there and are wired in
by ``Role``.
"""

from mote.runtime.context.turn.bus import TurnContextBus
from mote.runtime.context.turn.sources import (
    ChangedFilesContextSource,
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
    ToolsetInstructionsContextSource,
)

__all__ = [
    "TurnContextBus",
    "ChangedFilesContextSource",
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
    "ToolsetInstructionsContextSource",
]
