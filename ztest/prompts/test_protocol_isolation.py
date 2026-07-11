#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Protocol-isolation invariant: shared prose never leaks one protocol's syntax.

This is the structural guard that makes the recurring ``<end></end>`` leak class
*impossible to reintroduce silently* rather than merely fixed once. Shared prompt
prose names protocol mechanics only through ``⟦...⟧`` symbols
(``common.prompt.refs``); each CommandChannel lowers them to its own surface at
the end of assembly. These tests assert, across the registered prompt × protocol
matrix:

  1. every symbol used in shared prose has a surface in BOTH channels'
     vocabularies (lowering raises UnknownSymbolError otherwise),
  2. a lowered prompt carries no residual ``⟦...⟧`` token,
  3. a NATIVE render contains zero XML mechanics (``<end>`` etc.) — the leak we
     keep hitting — while the XML render still does,
  4. shared prose holds no RAW protocol literals (so a future author cannot
     bypass the symbol layer by typing the mechanic directly).

If someone adds a new shared prompt with a hard-coded ``<end></end>`` or a new
symbol with no vocabulary entry, one of these fails.
"""
from __future__ import annotations

from string import Template

import pytest
from mote.common import prompt as R
from mote.common.prompt.refs import Sym, UnknownSymbolError, find_symbols
from mote.parser.native_channel import NativeToolChannel
from mote.parser.xml_channel import XmlCommandChannel

# The shared prose templates that flow to BOTH protocols. Each is reduced to a
# concrete string (placeholders filled with dummies) so only protocol symbols,
# not ${...} template holes, remain to inspect.
_SHARED_PROMPTS = {
    "SYSTEM_PROMPT": R.SYSTEM_PROMPT,
    "AGENT_TASK_PROMPT": Template(R.AGENT_TASK_PROMPT).safe_substitute(parent_name="P", context="C", task="T"),
}

# Raw protocol-mechanic literals that must NOT appear in shared prose — the
# whole point is that these only ever materialize via channel lowering.
_XML_RAW_LITERALS = ["<end>", "</end>", "command block", "Editor.read", "Editor.write"]

_CHANNELS = {"xml": XmlCommandChannel(), "native": NativeToolChannel()}


class TestVocabularyCompleteness:
    def test_every_used_symbol_has_a_surface_in_both_channels(self):
        used: set[str] = set()
        for text in _SHARED_PROMPTS.values():
            used.update(find_symbols(text))
        assert used, "expected shared prose to use protocol symbols"
        for ch_name, ch in _CHANNELS.items():
            vocab_values = {(k.value if isinstance(k, Sym) else str(k)) for k in ch.vocabulary()}
            missing = used - vocab_values
            assert not missing, f"{ch_name} vocabulary missing surfaces for {missing}"

    def test_both_channels_cover_the_same_symbols(self):
        xml_keys = {k.value if isinstance(k, Sym) else str(k) for k in _CHANNELS["xml"].vocabulary()}
        nat_keys = {k.value if isinstance(k, Sym) else str(k) for k in _CHANNELS["native"].vocabulary()}
        assert xml_keys == nat_keys, "channels must define the same symbol set"


class TestNoResidualSymbols:
    @pytest.mark.parametrize("prompt_name", sorted(_SHARED_PROMPTS))
    @pytest.mark.parametrize("ch_name", sorted(_CHANNELS))
    def test_lowering_leaves_no_symbol(self, prompt_name, ch_name):
        lowered = _CHANNELS[ch_name].lower(_SHARED_PROMPTS[prompt_name])
        assert find_symbols(lowered) == [], f"{ch_name}.lower({prompt_name}) left symbols: {find_symbols(lowered)}"


class TestNativeNeverLeaksXmlMechanics:
    @pytest.mark.parametrize("prompt_name", sorted(_SHARED_PROMPTS))
    def test_native_render_has_no_end_marker(self, prompt_name):
        lowered = _CHANNELS["native"].lower(_SHARED_PROMPTS[prompt_name])
        assert "<end>" not in lowered
        assert "</end>" not in lowered
        # The dotted command-name mechanic is XML-only too.
        assert "Editor.read" not in lowered
        assert "command block" not in lowered

    def test_xml_render_does_carry_its_mechanics(self):
        # Sanity: the XML surface really does materialize <end></end> / command
        # block, proving the symbol was live (not silently dropped by both sides).
        lowered = _CHANNELS["xml"].lower(_SHARED_PROMPTS["AGENT_TASK_PROMPT"])
        assert "command block" in lowered
        lowered_sys = _CHANNELS["xml"].lower(_SHARED_PROMPTS["SYSTEM_PROMPT"])
        assert "Editor.read" in lowered_sys


class TestSharedProseHasNoRawLiterals:
    @pytest.mark.parametrize("prompt_name", sorted(_SHARED_PROMPTS))
    def test_no_raw_xml_literal_in_shared_prose(self, prompt_name):
        # The pre-lowering prose must reference mechanics ONLY via symbols, so a
        # future author cannot bypass the layer by typing the literal directly.
        text = _SHARED_PROMPTS[prompt_name]
        for literal in _XML_RAW_LITERALS:
            assert literal not in text, (
                f"{prompt_name} contains raw protocol literal {literal!r}; " f"use a ⟦...⟧ symbol instead"
            )


class TestLoweringFailsLoudOnBadSymbol:
    def test_unknown_symbol_raises(self):
        bad = f"do {Sym.CTL_FINISH} then \u27e6ctl:does_not_exist\u27e7"
        with pytest.raises(UnknownSymbolError):
            _CHANNELS["native"].lower(bad)
