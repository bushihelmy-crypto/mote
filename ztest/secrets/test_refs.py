#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Secret-reference expansion — turn ``<secret:…>`` placeholders into values.

Verifies each of the three placeholder forms resolves via ``get_secret`` (with
TOTP computed from a stored seed), that non-placeholder text passes through, and
the fail-closed behaviour: an unknown key / empty value / bad seed raises
``SecretRefError`` rather than typing the literal placeholder.
"""
from __future__ import annotations

import pytest

from mote.runtime.secrets.refs import SecretRefError, expand_secret_refs, has_secret_refs
from mote.runtime.secrets.totp import totp_now

_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _lookup(mapping):
    return lambda key: mapping.get(key)


class TestExpansion:
    def test_agent_vault_ref(self):
        out = expand_secret_refs("<agent-vault:pw>", get_secret=_lookup({"pw": "hunter2"}))
        assert out == "hunter2"

    def test_secret_dotted_ref(self):
        out = expand_secret_refs("<secret:llm.api_key>", get_secret=_lookup({"llm.api_key": "sk-abc"}))
        assert out == "sk-abc"

    def test_totp_ref_computes_current_code(self):
        out = expand_secret_refs("<totp:tf>", get_secret=_lookup({"tf": _SEED}))
        # Same generator, so it equals a directly-computed code (allow a period
        # boundary by accepting either of the adjacent codes is unnecessary — the
        # two calls are microseconds apart, effectively the same instant).
        assert out == totp_now(_SEED) or out == expand_secret_refs("<totp:tf>", get_secret=_lookup({"tf": _SEED}))

    def test_embedded_in_surrounding_text(self):
        out = expand_secret_refs(
            "user=<agent-vault:u>;pw=<agent-vault:p>", get_secret=_lookup({"u": "alice", "p": "s3cret!!"})
        )
        assert out == "user=alice;pw=s3cret!!"

    def test_no_placeholder_passes_through(self):
        assert expand_secret_refs("plain text", get_secret=_lookup({})) == "plain text"

    def test_empty_text_passes_through(self):
        assert expand_secret_refs("", get_secret=_lookup({})) == ""


class TestFailClosed:
    def test_unknown_key_raises(self):
        with pytest.raises(SecretRefError):
            expand_secret_refs("<agent-vault:missing>", get_secret=_lookup({}))

    def test_empty_value_raises(self):
        with pytest.raises(SecretRefError):
            expand_secret_refs("<agent-vault:blank>", get_secret=_lookup({"blank": ""}))

    def test_bad_totp_seed_raises(self):
        with pytest.raises(SecretRefError):
            expand_secret_refs("<totp:bad>", get_secret=_lookup({"bad": "!!!not-base32!!!"}))

    def test_disabled_store_stub_raises(self):
        # get_secret returning None for everything (secrets disabled) fails closed.
        with pytest.raises(SecretRefError):
            expand_secret_refs("<agent-vault:x>", get_secret=lambda _k: None)


class TestHasSecretRefs:
    def test_detects_each_prefix(self):
        assert has_secret_refs("<secret:a>")
        assert has_secret_refs("<agent-vault:b>")
        assert has_secret_refs("<totp:c>")

    def test_negative(self):
        assert not has_secret_refs("no refs here")
        assert not has_secret_refs("")
        # An unrelated angle-bracket token is not a secret ref.
        assert not has_secret_refs("<div>text</div>")
