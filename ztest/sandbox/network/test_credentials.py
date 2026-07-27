#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the credential broker (``sandbox.network.credentials``).

Pure logic — no I/O. Covers domain matching, the three header schemes, and the
fail-closed behaviour when a rule has no matching secret.
"""
from __future__ import annotations

import base64

from mote.runtime.sandbox.network.credentials import CredentialBroker, CredentialRule


def _lookup(mapping):
    """A secret_lookup closure over a plain dict."""
    return lambda key: mapping.get(key)


def test_rule_matches_glob_domains():
    rule = CredentialRule(domains=("**.github.com",), secret_key="gh")
    assert rule.matches("api.github.com")
    assert rule.matches("github.com")
    assert not rule.matches("evil.com")


def test_rule_no_match_empty_host():
    rule = CredentialRule(domains=("**.github.com",), secret_key="gh")
    assert not rule.matches("")


def test_broker_match_returns_first_matching_rule():
    r1 = CredentialRule(domains=("api.example.com",), secret_key="a")
    r2 = CredentialRule(domains=("**.github.com",), secret_key="b")
    broker = CredentialBroker([r1, r2], _lookup({"a": "x", "b": "y"}))
    assert broker.match("api.github.com") is r2
    assert broker.match("api.example.com") is r1
    assert broker.match("nomatch.org") is None


def test_header_for_bearer():
    broker = CredentialBroker(
        [CredentialRule(domains=("**.github.com",), secret_key="gh", scheme="bearer")],
        _lookup({"gh": "tok123"}),
    )
    assert broker.header_for("api.github.com") == ("Authorization", "Bearer tok123")


def test_header_for_basic():
    broker = CredentialBroker(
        [CredentialRule(domains=("git.example.com",), secret_key="pw", scheme="basic", username="alice")],
        _lookup({"pw": "s3cr3t"}),
    )
    name, value = broker.header_for("git.example.com")
    assert name == "Authorization"
    expected = "Basic " + base64.b64encode(b"alice:s3cr3t").decode("ascii")
    assert value == expected


def test_header_for_basic_no_username():
    broker = CredentialBroker(
        [CredentialRule(domains=("git.example.com",), secret_key="pw", scheme="basic")],
        _lookup({"pw": "s3cr3t"}),
    )
    _, value = broker.header_for("git.example.com")
    assert value == "Basic " + base64.b64encode(b":s3cr3t").decode("ascii")


def test_header_for_custom_header_scheme():
    broker = CredentialBroker(
        [CredentialRule(domains=("api.x.com",), secret_key="k", scheme="header", header="X-Api-Key")],
        _lookup({"k": "rawvalue"}),
    )
    assert broker.header_for("api.x.com") == ("X-Api-Key", "rawvalue")


def test_header_for_no_rule_returns_none():
    broker = CredentialBroker([CredentialRule(domains=("a.com",), secret_key="k")], _lookup({"k": "v"}))
    assert broker.header_for("other.com") is None


def test_header_for_missing_secret_fails_closed():
    """A matching rule whose secret is absent yields None — never a partial header."""
    broker = CredentialBroker(
        [CredentialRule(domains=("a.com",), secret_key="absent")],
        _lookup({}),
    )
    assert broker.header_for("a.com") is None


def test_header_for_empty_secret_fails_closed():
    broker = CredentialBroker(
        [CredentialRule(domains=("a.com",), secret_key="k")],
        _lookup({"k": ""}),
    )
    assert broker.header_for("a.com") is None


def test_should_intercept_is_lookup_independent():
    """should_intercept is decided by rule config, not whether the secret resolves."""
    broker = CredentialBroker([CredentialRule(domains=("a.com",), secret_key="absent")], _lookup({}))
    assert broker.should_intercept("a.com") is True  # even though header_for → None
    assert broker.should_intercept("other.com") is False


def test_intercept_hosts_lists_all_domains():
    broker = CredentialBroker(
        [
            CredentialRule(domains=("a.com", "b.com"), secret_key="k1"),
            CredentialRule(domains=("**.c.com",), secret_key="k2"),
        ],
        _lookup({}),
    )
    assert broker.intercept_hosts == ["a.com", "b.com", "**.c.com"]
