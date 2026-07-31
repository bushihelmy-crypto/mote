#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Authorization-code + PKCE login flow (RFC 6749 §4.1, RFC 7636).

CLI-oriented: spins a localhost HTTP server on the configured ``redirect_uri``
port to capture the ``?code&state`` redirect, validates ``state`` against the
value we generated, then exchanges the code for a token. Browser-less callers
get the authorize URL via ``LoginCallbacks.on_url``.
"""
from __future__ import annotations

import http.server
import threading
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.runtime.models.auth.oauth.client import OAuthClient
from mote.runtime.models.auth.oauth.errors import OAuthConfigError, OAuthRefreshError
from mote.runtime.models.auth.oauth.flows.callbacks import LoginCallbacks
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.pkce import gen_code_challenge, gen_code_verifier, gen_state
from mote.runtime.telemetry.logging import logger

_DEFAULT_TIMEOUT = 300.0  # seconds to wait for the browser redirect


def _build_authorize_url(config: OAuthProviderConfig, *, challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if config.scopes:
        params["scope"] = " ".join(config.scopes)
    return f"{config.authorize_url}?{urlencode(params)}"


def run_auth_code_flow(
    config: OAuthProviderConfig,
    callbacks: Optional[LoginCallbacks] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> OAuthToken:
    """Run the interactive authorization-code (PKCE) flow and return a token.

    Raises :class:`OAuthConfigError` when ``client_id`` / ``authorize_url`` are
    missing, and :class:`OAuthRefreshError` on timeout, callback error, or
    ``state`` mismatch.
    """
    callbacks = callbacks or LoginCallbacks()
    if not config.client_id:
        raise OAuthConfigError("authorization_code flow requires a 'client_id' (bring your own)")
    if not config.authorize_url:
        raise OAuthConfigError("authorization_code flow requires an 'authorize_url'")

    verifier = gen_code_verifier()
    challenge = gen_code_challenge(verifier)
    state = gen_state()

    parts = urlsplit(config.redirect_uri)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    callback_path = parts.path or "/callback"

    captured: dict = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            split = urlsplit(self.path)
            if split.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(split.query)
            captured["code"] = (qs.get("code") or [None])[0]
            captured["state"] = (qs.get("state") or [None])[0]
            captured["error"] = (qs.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body>Login complete. You can close this window.</body></html>")
            done.set()

        def log_message(self, *args):  # silence default stderr logging
            return

    server = http.server.HTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authorize_url = _build_authorize_url(config, challenge=challenge, state=state)
        callbacks.url(authorize_url)
        callbacks.progress("Waiting for authorization in your browser...")
        if not done.wait(timeout):
            raise OAuthRefreshError("timed out waiting for the authorization callback", recoverable=True)
    finally:
        server.shutdown()
        server.server_close()

    if captured.get("error"):
        raise OAuthRefreshError(f"authorization failed: {captured['error']}", recoverable=False)
    code = captured.get("code")
    if not code:
        raise OAuthRefreshError("authorization callback returned no code", recoverable=False)
    if captured.get("state") != state:
        raise OAuthRefreshError("OAuth state mismatch (possible CSRF)", recoverable=False)

    callbacks.progress("Exchanging authorization code for a token...")
    logger.debug("auth-code flow: exchanging code for token")
    return OAuthClient(config).exchange_code(code, verifier, config.redirect_uri)


__all__ = ["run_auth_code_flow"]
