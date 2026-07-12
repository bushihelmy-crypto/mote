#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the SSRF gate + domain allowlist (``sandbox.network.policy``).

Pure functions — no sockets, no subprocess. Covers host normalization, the
SSRF blocked-range table, glob pattern matching, and the composed ``allows``
decision (closed-by-default when no domains are allowed).
"""
from __future__ import annotations

import pytest

from metagpt.sandbox.network.policy import (
    NetworkPolicy,
    is_blocked_host,
    normalize_host,
)


class TestNormalizeHost:
    def test_lowercases_and_strips_port(self):
        assert normalize_host("API.Example.COM:443") == "api.example.com"

    def test_strips_trailing_dot(self):
        assert normalize_host("example.com.") == "example.com"

    def test_bracketed_ipv6_with_port(self):
        assert normalize_host("[::1]:8080") == "::1"

    def test_bracketed_ipv6_no_port(self):
        assert normalize_host("[2001:db8::1]") == "2001:db8::1"

    def test_bare_ipv6_keeps_colons(self):
        # Many colons -> not treated as host:port.
        assert normalize_host("2001:db8::1") == "2001:db8::1"

    def test_empty(self):
        assert normalize_host("") == ""
        assert normalize_host("   ") == ""


class TestIsBlockedHost:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",        # loopback
            "10.0.0.1",         # RFC1918
            "192.168.1.1",      # RFC1918
            "172.16.0.1",       # RFC1918
            "169.254.1.1",      # link-local
            "100.64.0.1",       # CGNAT
            "224.0.0.1",        # multicast
            "0.0.0.0",          # unspecified
            "192.0.2.5",        # TEST-NET-1
            "198.51.100.5",     # TEST-NET-2
            "203.0.113.5",      # TEST-NET-3
            "::1",              # ipv6 loopback
        ],
    )
    def test_internal_addresses_blocked(self, host):
        assert is_blocked_host(host) is True

    @pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_public_ip_not_blocked(self, host):
        assert is_blocked_host(host) is False

    def test_hostname_not_blocked_here(self):
        # Hostnames are gated by the allowlist, not the SSRF IP filter.
        assert is_blocked_host("example.com") is False

    def test_empty_host_blocked(self):
        assert is_blocked_host("") is True


class TestNetworkPolicyAllows:
    def test_empty_allowlist_denies_all(self):
        policy = NetworkPolicy([])
        assert policy.allows("example.com") is False
        assert policy.allows("8.8.8.8") is False

    def test_exact_match(self):
        policy = NetworkPolicy(["example.com"])
        assert policy.allows("example.com") is True
        assert policy.allows("api.example.com") is False

    def test_single_label_wildcard(self):
        policy = NetworkPolicy(["*.example.com"])
        assert policy.allows("api.example.com") is True
        # Apex does NOT match a single-label wildcard.
        assert policy.allows("example.com") is False
        # Two labels deep does NOT match.
        assert policy.allows("a.b.example.com") is False

    def test_deep_wildcard(self):
        policy = NetworkPolicy(["**.example.com"])
        assert policy.allows("example.com") is True
        assert policy.allows("api.example.com") is True
        assert policy.allows("a.b.example.com") is True

    def test_ssrf_gate_overrides_allowlist(self):
        # Even an allowed IP literal is rejected when it's a private address.
        policy = NetworkPolicy(["**.anything", "10.0.0.1"])
        assert policy.allows("10.0.0.1") is False

    def test_public_ip_requires_explicit_allow(self):
        policy = NetworkPolicy(["8.8.8.8"])
        assert policy.allows("8.8.8.8") is True
        assert policy.allows("1.1.1.1") is False
