#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the netns sole-egress builders (``network/enforce.py``).

All pure / dependency-light: no root, no namespace. They assert the *shape* of
the slirp argv, the nft ruleset, the inner prelude and the gateway URL. The
end-to-end enforcement (a real netns whose only egress is the proxy) is covered
in ``test_orchestrator.py`` behind a toolchain ``skipif``.
"""
from __future__ import annotations

from mote.sandbox.network import enforce


class TestEnforcementAvailable:
    def test_returns_bool(self):
        assert isinstance(enforce.enforcement_available(), bool)


class TestSlirpArgv:
    def test_carries_pid_tap_and_ready_fd(self):
        argv = enforce.build_slirp_argv(4242, ready_fd=7)
        assert argv[0] == "slirp4netns"
        assert "--configure" in argv
        assert "--ready-fd=7" in argv
        # pid + tap device are the trailing positional args.
        assert argv[-2:] == ["4242", enforce.TAP_DEVICE]

    def test_mtu_flag_present(self):
        argv = enforce.build_slirp_argv(1, ready_fd=3)
        assert any(a.startswith("--mtu=") for a in argv)


class TestNftRuleset:
    def test_default_drop_and_proxy_allow(self):
        rs = enforce.build_nft_ruleset(8080)
        assert "policy drop" in rs
        # Loopback + established + DNS + the proxy gateway:port are allowed.
        assert 'oif "lo" accept' in rs
        assert "ct state established,related accept" in rs
        assert "udp dport 53 accept" in rs
        assert f"ip daddr {enforce.SLIRP_GATEWAY} tcp dport 8080 accept" in rs

    def test_port_is_interpolated(self):
        assert "tcp dport 9999 accept" in enforce.build_nft_ruleset(9999)
        assert "tcp dport 9999 accept" not in enforce.build_nft_ruleset(8888)


class TestProxyUrlInNetns:
    def test_uses_gateway_not_loopback(self):
        url = enforce.proxy_url_in_netns(5555)
        assert url == f"http://{enforce.SLIRP_GATEWAY}:5555"
        assert "127.0.0.1" not in url


class TestInnerPrelude:
    def test_brings_lo_up_waits_route_locks_then_execs(self):
        prelude = enforce.build_inner_prelude(7000)
        # lo up.
        assert "ip link set lo up" in prelude
        # waits for slirp's default route via the gateway.
        assert enforce.SLIRP_GATEWAY in prelude
        assert "ip route show default" in prelude
        # installs the nft lock (heredoc-fed).
        assert "nft -f -" in prelude
        assert "tcp dport 7000 accept" in prelude
        # finally execs the real payload (no lingering wrapper).
        assert 'exec "$@"' in prelude

    def test_tolerant_of_failures(self):
        # nft / ip failures must not abort the payload (best-effort lock).
        prelude = enforce.build_inner_prelude(7000)
        assert "|| true" in prelude

    def test_drops_capabilities_before_exec(self):
        # After installing the lock the prelude must surrender all capabilities
        # (incl. CAP_NET_ADMIN) so the payload cannot `nft flush` the lock away.
        prelude = enforce.build_inner_prelude(7000)
        assert 'capsh --caps="" --' in prelude
        # The cap drop must come AFTER the nft lock is installed (you need
        # CAP_NET_ADMIN to install it) and BEFORE control passes to the payload.
        assert prelude.index("nft -f -") < prelude.index("capsh")

    def test_cap_drop_threads_argv_through(self):
        # The double-exec must re-pass the positional params so the real argv
        # survives the capsh layer untouched.
        prelude = enforce.build_inner_prelude(7000)
        assert 'exec capsh --caps="" -- -c \'exec "$@"\' sbx "$@"' in prelude

    def test_cap_drop_is_best_effort(self):
        # capsh may be absent on a minimal host — the prelude must still exec the
        # payload directly (lock installed but tamper-droppable; degraded, never
        # a hard break).
        prelude = enforce.build_inner_prelude(7000)
        assert "command -v capsh" in prelude
        # A bare `exec "$@"` fallback exists outside the capsh branch.
        assert prelude.rstrip().endswith('exec "$@"')
