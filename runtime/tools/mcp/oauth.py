"""Bridge the shared OAuth runtime onto fastmcp's HTTP client auth.

``executor.mcp`` (the ``UniversalMCP`` manager + fastmcp ``Client``) and
``router.oauth`` (``OAuthManager`` — token mint/refresh, cross-process file
lock, keyring/file storage) already exist but were never connected: a remote
MCP server that requires a bearer token had no way to obtain one. This module
is the thin seam between them.

A ``MCPServerConfig.oauth`` block (parsed from the standard ``mcp.json`` server
entry) is fed to an :class:`~mote.runtime.models.auth.oauth.OAuthManager` exactly as the LLM
providers do (``router.llm.credentials``); the manager is then wrapped in a
refreshing ``httpx.Auth`` (:class:`_OAuthManagerAuth`) and handed to the
``Client`` at build time. The auth consults the manager on *every* request
(not a one-time token snapshot), so the manager's proactive refresh keeps a
long-lived, cached client authenticated across token expiry.

Only remote (SSE/HTTP) servers authenticate this way — STDIO servers are local
processes with no HTTP auth surface. No auth configured => ``None`` (unchanged,
unauthenticated client).
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Generator, Optional

import httpx

from mote.contracts.config.mcp import MCPServerConfig
from mote.runtime.models.auth.oauth import OAuthManager


class _OAuthManagerAuth(httpx.Auth):
    """An ``httpx.Auth`` that pulls a fresh bearer from an :class:`OAuthManager`.

    Unlike a static token snapshot, this consults the manager on every request,
    so the manager's proactive refresh (memoized fast path; file-locked network
    refresh only near expiry) keeps a long-lived, cached MCP client
    authenticated across token expiry — no mid-session 401. The async flow
    offloads the (potentially blocking) token fetch to a worker thread so a
    refresh never stalls the event loop.
    """

    def __init__(self, manager: OAuthManager) -> None:
        self._manager = manager

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._manager.get_valid_token()}"
        yield request

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await asyncio.to_thread(self._manager.get_valid_token)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def build_mcp_auth(server_config: MCPServerConfig) -> Optional[httpx.Auth]:
    """Return a refreshing ``httpx.Auth`` for ``server_config``, or ``None``.

    ``None`` when the server declares no ``oauth`` block (client stays
    unauthenticated). Otherwise an :class:`~mote.runtime.models.auth.oauth.OAuthManager` is
    built from the block and wrapped in :class:`_OAuthManagerAuth`. Mirrors
    ``router.llm.credentials._build_oauth_manager`` — one OAuth runtime, reused
    verbatim.
    """
    oauth = server_config.oauth
    if oauth is None:
        return None

    manager = OAuthManager(oauth, provider=server_config.name)
    return _OAuthManagerAuth(manager)


__all__ = ["build_mcp_auth"]
