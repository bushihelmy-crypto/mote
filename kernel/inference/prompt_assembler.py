"""Unique validated assembly point for prompt sections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from string import Template

from mote.contracts.conversation.prompt import PromptRegion, PromptSection, PromptSectionIdentity, ProtocolVocabulary


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    static_prefix: str
    system_dynamic: str
    turn_content: str
    section_set_fingerprint: str
    vocabulary_fingerprint: str


class PromptAssembler:
    def __init__(self, owner_namespaces: dict[str, frozenset[str]]) -> None:
        self._owners = dict(owner_namespaces)
        self._sections: dict[PromptSectionIdentity, PromptSection] = {}

    def register(self, section: PromptSection) -> None:
        identity = section.identity
        allowed = self._owners.get(identity.owner_capability)
        if allowed is None or identity.section_key not in allowed:
            raise ValueError("prompt section owner or namespace is not registered")
        if identity in self._sections:
            raise ValueError("duplicate prompt section identity")
        if identity.region is PromptRegion.STATIC and Template.pattern.search(section.content):
            raise ValueError("static prompt sections must not contain placeholders")
        self._sections[identity] = section

    def assemble(self, vocabulary: ProtocolVocabulary) -> AssembledPrompt:
        region_order = {
            PromptRegion.STATIC: 0,
            PromptRegion.SYSTEM_DYNAMIC: 1,
            PromptRegion.TURN: 2,
        }
        sections = sorted(
            self._sections.values(),
            key=lambda item: (region_order[item.identity.region], item.order, item.identity),
        )
        static = "\n".join(item.content for item in sections if item.identity.region is PromptRegion.STATIC)
        dynamic = "\n".join(item.content for item in sections if item.identity.region is PromptRegion.SYSTEM_DYNAMIC)
        turn = "\n".join(item.content for item in sections if item.identity.region is PromptRegion.TURN)
        identities = "\n".join(
            f"{item.identity.owner_capability}:{item.identity.section_key}:"
            f"{item.identity.region.value}:{item.identity.version}:{item.history_policy}"
            for item in sections
        )
        fingerprint = hashlib.sha256(identities.encode()).hexdigest()
        return AssembledPrompt(static, dynamic, turn, fingerprint, vocabulary.fingerprint)


__all__ = ["AssembledPrompt", "PromptAssembler"]
