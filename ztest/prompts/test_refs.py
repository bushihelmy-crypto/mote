#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for kernel.prompt.refs — the protocol-symbol primitive.

These cover the lowerer in isolation (the channel-agnostic core): symbol token
rendering, ``lower`` substitution + ``UnknownSymbolError``, ``find_symbols``,
``assert_no_symbols``, and ``normalize_vocabulary`` key coercion. The channel /
prompt-matrix invariants live in test_protocol_isolation.py.
"""
from __future__ import annotations

import pytest

from mote.kernel.commands.symbols import (
    CAP_READ,
    CTL_FINISH,
    Sym,
    UnknownSymbolError,
    assert_no_symbols,
    find_symbols,
    lower,
    normalize_vocabulary,
)

_OPEN = "\u27e6"
_CLOSE = "\u27e7"


class TestSymbolTokens:
    def test_member_str_is_bracketed_value(self):
        assert str(Sym.CTL_FINISH) == f"{_OPEN}ctl:finish{_CLOSE}"

    def test_public_constants_match_members(self):
        assert CTL_FINISH == str(Sym.CTL_FINISH)
        assert CAP_READ == str(Sym.CAP_READ)

    def test_symbols_use_uncommon_brackets(self):
        # The whole scheme relies on these chars never appearing in prose/code.
        assert _OPEN not in "normal prose, JSON {}, code(), <xml/>"
        assert _CLOSE not in "normal prose, JSON {}, code(), <xml/>"


class TestFindSymbols:
    def test_finds_all_inner_names(self):
        text = f"a {CTL_FINISH} b {CAP_READ} c"
        assert find_symbols(text) == ["ctl:finish", "cap:read"]

    def test_empty_and_none_safe(self):
        assert find_symbols("") == []
        assert find_symbols(None) == []  # type: ignore[arg-type]

    def test_no_symbols_returns_empty(self):
        assert find_symbols("plain text") == []


class TestLower:
    def test_substitutes_known_symbols(self):
        vocab = {"ctl:finish": "DONE", "cap:read": "READ"}
        assert lower(f"{CTL_FINISH}/{CAP_READ}", vocab) == "DONE/READ"

    def test_unknown_symbol_raises(self):
        with pytest.raises(UnknownSymbolError):
            lower(CTL_FINISH, {"cap:read": "READ"})

    def test_no_symbols_is_identity(self):
        assert lower("nothing here", {}) == "nothing here"

    def test_empty_text_is_identity(self):
        assert lower("", {"ctl:finish": "x"}) == ""

    def test_repeated_symbol_all_replaced(self):
        vocab = {"ctl:finish": "X"}
        assert lower(f"{CTL_FINISH} and {CTL_FINISH}", vocab) == "X and X"


class TestAssertNoSymbols:
    def test_clean_text_passes(self):
        assert_no_symbols("all lowered already")  # no raise

    def test_residual_symbol_raises_with_location(self):
        with pytest.raises(UnknownSymbolError) as ei:
            assert_no_symbols(f"oops {CTL_FINISH}", where="system_prompt")
        assert "system_prompt" in str(ei.value)


class TestNormalizeVocabulary:
    def test_coerces_sym_keys_to_values(self):
        out = normalize_vocabulary({Sym.CTL_FINISH: "X", Sym.CAP_READ: "Y"})
        assert out == {"ctl:finish": "X", "cap:read": "Y"}

    def test_passes_string_keys_through(self):
        out = normalize_vocabulary({"ctl:finish": "X"})
        assert out == {"ctl:finish": "X"}

    def test_mixed_keys(self):
        out = normalize_vocabulary({Sym.CTL_FINISH: "X", "cap:read": "Y"})
        assert out == {"ctl:finish": "X", "cap:read": "Y"}
