from __future__ import annotations

import pytest

from mote.contracts.conversation.prompt import PromptRegion, PromptSection, PromptSectionIdentity, ProtocolVocabulary
from mote.kernel.inference.prompt_assembler import PromptAssembler


def _section(owner="commands", key="protocol", region=PromptRegion.STATIC, content="fixed"):
    return PromptSection(PromptSectionIdentity(owner, key, region, "1"), content)


def test_prompt_owner_and_duplicate_identity_are_enforced():
    assembler = PromptAssembler({"commands": frozenset({"protocol"})})
    with pytest.raises(ValueError, match="owner or namespace"):
        assembler.register(_section(owner="product"))
    assembler.register(_section())
    with pytest.raises(ValueError, match="duplicate"):
        assembler.register(_section())


def test_static_section_rejects_placeholders_but_turn_section_allows_them():
    assembler = PromptAssembler({"commands": frozenset({"protocol", "turn"})})
    with pytest.raises(ValueError, match="placeholders"):
        assembler.register(_section(content="hello $name"))
    assembler.register(_section(key="turn", region=PromptRegion.TURN, content="hello $name"))


def test_assembly_fingerprint_covers_history_policy():
    vocabulary = ProtocolVocabulary("xml", "1", (), "vocab")
    first = PromptAssembler({"commands": frozenset({"protocol"})})
    second = PromptAssembler({"commands": frozenset({"protocol"})})
    first.register(_section())
    second.register(PromptSection(_section().identity, "fixed", history_policy="persistent"))

    assert first.assemble(vocabulary).section_set_fingerprint != second.assemble(vocabulary).section_set_fingerprint
