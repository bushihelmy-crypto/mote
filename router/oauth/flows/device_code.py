#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Device-authorization login flow (RFC 8628).

For headless / no-browser-redirect providers (e.g. GitHub Copilot): request a
device + user code, surface the user code and verification URL to the user via
``LoginCallbacks.on_device_code``, then poll the token endpoint to completion.
"""
from __future__ import annotations

from typing import Optional

from mote.common.config.config.oauth_config import OAuthProviderConfig
from mote.router.oauth.client import OAuthClient
from mote.router.oauth.errors import OAuthConfigError
from mote.router.oauth.flows.callbacks import LoginCallbacks
from mote.router.oauth.models import OAuthToken


def run_device_code_flow(
    config: OAuthProviderConfig,
    callbacks: Optional[LoginCallbacks] = None,
) -> OAuthToken:
    """Run the interactive device-code flow and return a token.

    Raises :class:`OAuthConfigError` when ``client_id`` /
    ``device_authorization_url`` are missing.
    """
    callbacks = callbacks or LoginCallbacks()
    if not config.client_id:
        raise OAuthConfigError("device_code flow requires a 'client_id' (bring your own)")

    client = OAuthClient(config)
    info = client.request_device_code()
    callbacks.device_code(info)
    callbacks.progress(f"Open {info.verification_uri} and enter code {info.user_code}")
    return client.poll_device_token(info.device_code, interval=info.interval, expires_in=info.expires_in)


__all__ = ["run_device_code_flow"]
