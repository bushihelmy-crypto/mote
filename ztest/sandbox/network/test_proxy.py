#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Integration tests for the local egress proxy (``sandbox.network.proxy``).

Drives the asyncio proxy against a fake loopback upstream HTTP server. Because
the SSRF gate blocks loopback by default, the tests use a NetworkPolicy whose
``allows`` is monkeypatched / subclassed to permit the test upstream host while
still exercising the deny path for non-allowed hosts.

All within a single ``asyncio.run`` per test (no pytest-asyncio dependency).
"""
from __future__ import annotations

import asyncio

from mote.sandbox.network.policy import NetworkPolicy
from mote.sandbox.network.proxy import EgressProxy


class _AllowHosts(NetworkPolicy):
    """Policy that allows an explicit set of hosts (bypassing the SSRF gate).

    Used only in tests so we can point the proxy at a loopback fake upstream
    (which the real SSRF gate would reject).
    """

    def __init__(self, hosts):
        super().__init__(allowed_domains=list(hosts))
        self._hosts = set(hosts)

    def allows(self, host: str) -> bool:  # type: ignore[override]
        return host in self._hosts


async def _start_fake_upstream():
    """A trivial HTTP/1.1 server that replies 200 with a fixed body."""

    async def handle(reader, writer):
        # Read request headers (until blank line).
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        body = b"hello-from-upstream"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _http_get_via_proxy(proxy_port: int, host: str, upstream_port: int):
    """Send an absolute-form GET through the proxy; return (status_line, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    target = f"http://{host}:{upstream_port}/"
    writer.write(f"GET {target} HTTP/1.1\r\n".encode() + f"Host: {host}\r\n".encode() + b"Connection: close\r\n\r\n")
    await writer.drain()
    data = await reader.read(-1)
    writer.close()
    text = data.decode("latin1")
    status_line = text.split("\r\n", 1)[0]
    body = text.split("\r\n\r\n", 1)[-1]
    return status_line, body


def test_proxy_forwards_allowed_host():
    async def scenario():
        upstream, up_port = await _start_fake_upstream()
        proxy = EgressProxy(_AllowHosts(["127.0.0.1"]), validate_resolved_ips=False)
        await proxy.start()
        try:
            status, body = await _http_get_via_proxy(proxy.port, "127.0.0.1", up_port)
            assert "200" in status
            assert "hello-from-upstream" in body
        finally:
            await proxy.shutdown()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(scenario())


def test_proxy_rejects_denied_host():
    async def scenario():
        upstream, up_port = await _start_fake_upstream()
        # Allow nothing -> the proxy 403s every host.
        proxy = EgressProxy(_AllowHosts([]), validate_resolved_ips=False)
        await proxy.start()
        try:
            status, _ = await _http_get_via_proxy(proxy.port, "127.0.0.1", up_port)
            assert "403" in status
        finally:
            await proxy.shutdown()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(scenario())


def test_proxy_url_property():
    async def scenario():
        proxy = EgressProxy(_AllowHosts([]))
        await proxy.start()
        try:
            assert proxy.url == f"http://127.0.0.1:{proxy.port}"
            assert proxy.port > 0
        finally:
            await proxy.shutdown()

    asyncio.run(scenario())


# --- resolve → validate → pin (DNS-rebinding / SSRF-via-allowlist defence) ---


def _resolver(addrs):
    """Build an injectable resolver that always returns *addrs*."""

    async def resolve(host, port):
        return list(addrs)

    return resolve


def test_resolve_and_pin_returns_first_public_ip():
    """A host resolving to public IPs is pinned to the first validated one."""

    async def scenario():
        proxy = EgressProxy(_AllowHosts(["example.com"]), resolver=_resolver(["93.184.216.34", "93.184.216.35"]))
        assert await proxy._resolve_and_pin("example.com", 443) == "93.184.216.34"

    asyncio.run(scenario())


def test_resolve_and_pin_blocks_internal_ip():
    """An allowlisted name resolving to an internal address is rejected (SSRF)."""

    async def scenario():
        # Cloud metadata endpoint — link-local, must never be dialed.
        proxy = EgressProxy(_AllowHosts(["evil.test"]), resolver=_resolver(["169.254.169.254"]))
        assert await proxy._resolve_and_pin("evil.test", 80) is None

    asyncio.run(scenario())


def test_resolve_and_pin_fails_closed_on_mixed_addresses():
    """A rebinding set mixing public + private addresses is rejected wholesale."""

    async def scenario():
        proxy = EgressProxy(_AllowHosts(["evil.test"]), resolver=_resolver(["93.184.216.34", "127.0.0.1"]))
        assert await proxy._resolve_and_pin("evil.test", 80) is None

    asyncio.run(scenario())


def test_resolve_and_pin_denies_unresolvable_host():
    async def scenario():
        proxy = EgressProxy(_AllowHosts(["nope.test"]), resolver=_resolver([]))
        assert await proxy._resolve_and_pin("nope.test", 80) is None

    asyncio.run(scenario())


def test_resolve_and_pin_denies_on_resolver_error():
    async def scenario():
        async def boom(host, port):
            raise OSError("dns down")

        proxy = EgressProxy(_AllowHosts(["x.test"]), resolver=boom)
        assert await proxy._resolve_and_pin("x.test", 80) is None

    asyncio.run(scenario())


def test_resolve_and_pin_bypass_returns_host_unchanged():
    """validate_resolved_ips=False skips resolution entirely (test/loopback seam)."""

    async def scenario():
        proxy = EgressProxy(_AllowHosts(["127.0.0.1"]), validate_resolved_ips=False)
        assert await proxy._resolve_and_pin("127.0.0.1", 80) == "127.0.0.1"

    asyncio.run(scenario())


def test_proxy_rejects_allowlisted_host_resolving_internal():
    """End-to-end: a host that PASSES the allowlist but resolves to an internal
    address is 403'd — the resolved-IP gate closes the SSRF-via-allowlist hole.
    """

    async def scenario():
        proxy = EgressProxy(_AllowHosts(["metadata.test"]), resolver=_resolver(["169.254.169.254"]))
        await proxy.start()
        try:
            status, _ = await _http_get_via_proxy(proxy.port, "metadata.test", 80)
            assert "403" in status
        finally:
            await proxy.shutdown()

    asyncio.run(scenario())


def test_proxy_dials_pinned_ip_end_to_end(monkeypatch):
    """The connection dials the *resolved/pinned* IP, not the hostname.

    The injected resolver maps a fake hostname to the loopback upstream's IP;
    with the SSRF table neutralised for this test, the proxy must pin to that IP
    and reach the upstream — proving the dial uses the pinned address.
    """
    import mote.sandbox.network.proxy as proxy_mod

    async def scenario():
        upstream, up_port = await _start_fake_upstream()
        # Neutralise the SSRF table so the loopback pin validates (the point of
        # this test is the *pin/dial* wiring, not the IP-range check).
        monkeypatch.setattr(proxy_mod, "is_blocked_host", lambda _ip: False)
        proxy = EgressProxy(_AllowHosts(["upstream.test"]), resolver=_resolver(["127.0.0.1"]))
        await proxy.start()
        try:
            status, body = await _http_get_via_proxy(proxy.port, "upstream.test", up_port)
            assert "200" in status
            assert "hello-from-upstream" in body
        finally:
            await proxy.shutdown()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(scenario())
