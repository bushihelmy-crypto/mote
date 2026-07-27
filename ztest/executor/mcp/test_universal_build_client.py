#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``UniversalMCP._build_client`` auth injection.

Pins the seam where the OAuth bridge meets the fastmcp client: an SSE server
with an ``oauth`` block gets a refreshing ``httpx.Auth`` on its transport; a
plain SSE server gets none; STDIO (local process) never carries HTTP auth.
"""
from __future__ import annotations

from mote.contracts.config.mcp import MCPServerConfig, MCPTransportType
from mote.contracts.config.oauth import OAuthProviderConfig
from mote.runtime.tools.mcp import oauth as oauth_bridge
from mote.runtime.tools.mcp.oauth import _OAuthManagerAuth
from mote.runtime.tools.mcp.universal import UniversalMCP


class _FakeManager:
    def __init__(self, config, *, provider=None, **_):
        pass

    def get_valid_token(self) -> str:
        return "tok-xyz"


def test_sse_with_oauth_sets_auth_on_transport(monkeypatch):
    monkeypatch.setattr(oauth_bridge, "OAuthManager", _FakeManager)
    cfg = MCPServerConfig(
        name="remote",
        type=MCPTransportType.SSE,
        enabled=True,
        url="https://x/mcp",
        oauth=OAuthProviderConfig(token_url="https://example.com/oauth/token"),
    )
    client = UniversalMCP()._build_client(cfg)

    assert isinstance(client.transport.auth, _OAuthManagerAuth)


def test_sse_without_oauth_has_no_auth():
    cfg = MCPServerConfig(name="plain", type=MCPTransportType.SSE, enabled=True, url="https://x/mcp")
    client = UniversalMCP()._build_client(cfg)
    assert getattr(client.transport, "auth", None) is None


def test_stdio_ignores_oauth(monkeypatch):
    # A STDIO server config is a local process; even if an oauth block slipped
    # in, the STDIO branch never applies HTTP auth (no transport.auth surface).
    monkeypatch.setattr(oauth_bridge, "OAuthManager", _FakeManager)
    cfg = MCPServerConfig(
        name="local",
        type=MCPTransportType.STDIO,
        enabled=True,
        command="npx",
        args=["-y", "server"],
    )
    client = UniversalMCP()._build_client(cfg)
    # Not an HTTP transport -> no BearerAuth wired.
    assert getattr(client.transport, "auth", None) is None
