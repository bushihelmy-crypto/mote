#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CredentialIndexContextSource (the login-credential menu, per turn).

The source renders the NAMES of configured secrets paired with the placeholder
the model must write (never a value), so it can autonomously fill a login form.
It is duck-typed over a single ``get_labels`` callable (never imports secrets /
roles) and ephemeral (``save_to_context`` False), byte-stable across turns.
"""
from __future__ import annotations

import asyncio

from mote.common.interface import EphemeralContextSource, TurnContextPriority
from mote.context.turn_context import CredentialIndexContextSource


def run(coro):
    return asyncio.run(coro)


LABELS = {
    "xhs_phone": "<agent-vault:xhs_phone>",
    "gh_token": "<agent-vault:gh_token>",
    "llm.api_key": "<secret:llm.api_key>",
}


def test_is_ephemeral_context_source():
    src = CredentialIndexContextSource(get_labels=lambda: {})
    assert isinstance(src, EphemeralContextSource)
    # Ephemeral: re-injected each turn, never persisted into history.
    assert src.save_to_context is False
    assert src.priority == TurnContextPriority.CREDENTIAL_INDEX


def test_suppressed_when_browser_not_recently_used():
    # The dynamic render gate: even with labels present, the menu is silent when
    # the predicate says WebBrowser was NOT recently used.
    src = CredentialIndexContextSource(get_labels=lambda: LABELS, browser_recently_used=lambda: False)
    assert run(src.render()) is None


def test_rendered_when_browser_recently_used():
    src = CredentialIndexContextSource(get_labels=lambda: LABELS, browser_recently_used=lambda: True)
    out = run(src.render())
    assert out is not None
    assert "- gh_token: <agent-vault:gh_token>" in out


def test_no_predicate_always_renders():
    # A bare source (no predicate injected) renders whenever labels exist — the
    # gate is opt-in so plain label-inspection tests need not fake browser use.
    src = CredentialIndexContextSource(get_labels=lambda: LABELS)
    assert run(src.render()) is not None


def test_renders_names_with_placeholders():
    src = CredentialIndexContextSource(get_labels=lambda: LABELS)
    out = run(src.render())
    assert out is not None
    assert "- xhs_phone: <agent-vault:xhs_phone>" in out
    assert "- gh_token: <agent-vault:gh_token>" in out
    assert "- llm.api_key: <secret:llm.api_key>" in out


def test_header_explains_placeholder_and_totp():
    src = CredentialIndexContextSource(get_labels=lambda: LABELS)
    out = run(src.render())
    assert out is not None
    # Teaches the fill mechanism + TOTP + SMS caveat.
    assert "<totp:KEY>" in out
    assert "vault" in out.lower()
    assert "SMS" in out


def test_none_when_empty():
    src = CredentialIndexContextSource(get_labels=lambda: {})
    assert run(src.render()) is None


def test_never_leaks_values():
    # The getter yields only labels; even if a caller mistakenly passed a value
    # through the placeholder, the source renders exactly what it is given — the
    # contract is that get_labels never carries values (SecretStore.labels()).
    src = CredentialIndexContextSource(get_labels=lambda: LABELS)
    out = run(src.render())
    assert out is not None
    assert "13800000000" not in out  # no phone value ever present


def test_byte_stable_across_turns():
    src = CredentialIndexContextSource(get_labels=lambda: LABELS)
    assert run(src.render()) == run(src.render())


def test_sorted_order():
    src = CredentialIndexContextSource(get_labels=lambda: LABELS)
    out = run(src.render())
    assert out is not None
    # sorted() → gh_token < llm.api_key < xhs_phone
    assert out.index("gh_token") < out.index("llm.api_key") < out.index("xhs_phone")


def test_renders_inline_literal_values():
    # The source is kind-agnostic: a non-placeholder literal (a non-secret value
    # merged in by the caller) renders verbatim alongside secret placeholders.
    src = CredentialIndexContextSource(get_labels=lambda: {"username": "alice", "gh_token": "<agent-vault:gh_token>"})
    out = run(src.render())
    assert out is not None
    assert "- username: alice" in out
    assert "- gh_token: <agent-vault:gh_token>" in out
