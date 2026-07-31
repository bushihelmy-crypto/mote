"""Stable prompt section contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mote.contracts.tool.vocabulary import ProtocolVocabulary


class PromptRegion(str, Enum):
    STATIC = "static"
    SYSTEM_DYNAMIC = "system_dynamic"
    TURN = "turn"


@dataclass(frozen=True, slots=True, order=True)
class PromptSectionIdentity:
    owner_capability: str
    section_key: str
    region: PromptRegion
    version: str


@dataclass(frozen=True, slots=True)
class PromptSection:
    identity: PromptSectionIdentity
    content: str
    order: int = 0
    history_policy: str = "ephemeral"


__all__ = ["PromptRegion", "PromptSection", "PromptSectionIdentity", "ProtocolVocabulary"]
