#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the MCP OAuth bridge (``executor.mcp.oauth``).

The bridge turns a server's optional ``oauth`` block into a *refreshing*
``httpx.Auth`` backed by the shared ``router.oauth.OAuthManager``. These tests
pin the contract: no oauth => None; oauth => an ``httpx.Auth`` that consults the
manager on every request (so proactive refresh keeps a cached client alive), and
that manager is keyed by the server name. The manager is monkeypatched so
nothing touches the network or token storage.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.contracts.tool.transport import MCPTransportType
from mote.runtime.config.mcp import MCPServerConfig
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.tools.mcp import oauth as oauth_bridge
from mote.runtime.tools.mcp.oauth import _OAuthManagerAuth, build_mcp_auth


class _FakeManager:
    """Stand-in for OAuthManager: records its construction, serves a token.

    ``get_valid_token`` returns a value that advances each call so a test can
    prove the auth re-consults the manager per request (rather than snapshotting
    once at build time).
    """

    last_config = None
    last_provider = None

    def __init__(self, config, *, provider=None, **_):
        type(self).last_config = config
        type(self).last_provider = provider
        self._n = 0

    class _Borrow:
        def __init__(self, token: str) -> None:
            self.token = OAuthToken(access_token=token)

    def acquire_valid_borrow(self, *, expires_at):
        self._n += 1
        return self._Borrow(f"tok-{self._n}")

    def release_borrow(self, borrow) -> None:
        return None


def _oauth_cfg() -> OAuthProviderConfig:
    # A minimal valid config: token_url satisfies the _require_token_url validator.
    return OAuthProviderConfig(
        token_url="https://example.com/oauth/token",
        storage_root=Path("/approved/oauth"),
    )


def test_no_oauth_returns_none():
    cfg = MCPServerConfig(name="plain", type=MCPTransportType.SSE, enabled=True, url="https://x/sse")
    assert build_mcp_auth(cfg) is None


def test_oauth_builds_refreshing_auth(monkeypatch):
    monkeypatch.setattr(oauth_bridge, "OAuthManager", _FakeManager)
    cfg = MCPServerConfig(
        name="remote",
        type=MCPTransportType.SSE,
        enabled=True,
        url="https://x/sse",
        oauth=_oauth_cfg(),
    )
    auth = build_mcp_auth(cfg)
    assert isinstance(auth, _OAuthManagerAuth)


def test_manager_built_from_server_oauth_and_name(monkeypatch):
    monkeypatch.setattr(oauth_bridge, "OAuthManager", _FakeManager)
    cfg = _oauth_cfg()
    server = MCPServerConfig(
        name="mysrv",
        type=MCPTransportType.SSE,
        enabled=True,
        url="https://x/sse",
        oauth=cfg,
    )
    build_mcp_auth(server)
    # The manager is fed the server's oauth block, keyed by the server name so
    # its token store / lock path is stable per server.
    assert _FakeManager.last_config is cfg
    assert _FakeManager.last_provider == "mysrv"


def test_sync_flow_sets_authorization_header(monkeypatch):
    # The sync auth_flow must inject "Authorization: Bearer <token>".
    monkeypatch.setattr(oauth_bridge, "OAuthManager", _FakeManager)
    cfg = MCPServerConfig(
        name="remote",
        type=MCPTransportType.SSE,
        enabled=True,
        url="https://x/sse",
        oauth=_oauth_cfg(),
    )
    auth = build_mcp_auth(cfg)
    request = httpx.Request("GET", "https://x/sse")
    flow = auth.auth_flow(request)
    authed = next(flow)
    assert authed.headers["Authorization"] == "Bearer tok-1"
    flow.close()


def test_auth_reconsults_manager_each_request(monkeypatch):
    # The whole point of the refreshing auth: it does NOT snapshot a token at
    # build time; each request re-reads the manager's current valid token, so a
    # long-lived cached client picks up refreshes.
    monkeypatch.setattr(oauth_bridge, "OAuthManager", _FakeManager)
    cfg = MCPServerConfig(
        name="remote",
        type=MCPTransportType.SSE,
        enabled=True,
        url="https://x/sse",
        oauth=_oauth_cfg(),
    )
    auth = build_mcp_auth(cfg)
    first_flow = auth.auth_flow(httpx.Request("GET", "https://x/sse"))
    first = next(first_flow)
    first_flow.close()
    second_flow = auth.auth_flow(httpx.Request("GET", "https://x/sse"))
    second = next(second_flow)
    second_flow.close()
    assert first.headers["Authorization"] == "Bearer tok-1"
    assert second.headers["Authorization"] == "Bearer tok-2"


@pytest.mark.asyncio
async def test_async_flow_sets_authorization_header(monkeypatch):
    # The async flow offloads the (blocking) token fetch to a thread but must
    # still inject the same bearer header.
    monkeypatch.setattr(oauth_bridge, "OAuthManager", _FakeManager)
    cfg = MCPServerConfig(
        name="remote",
        type=MCPTransportType.SSE,
        enabled=True,
        url="https://x/sse",
        oauth=_oauth_cfg(),
    )
    auth = build_mcp_auth(cfg)

    request = httpx.Request("GET", "https://x/sse")
    flow = auth.async_auth_flow(request)
    authed = await flow.__anext__()
    await flow.aclose()
    assert authed.headers["Authorization"] == "Bearer tok-1"
