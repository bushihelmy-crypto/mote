#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Minimal, signature-agnostic JWT decoding helpers.

We only need to *read* claims (``exp``/``email``/account) from access tokens we
already trust because they came from the configured token endpoint over TLS.
We deliberately do NOT verify signatures here (no key material in P1).
"""
from __future__ import annotations

import base64
import json
from typing import Dict

from metagpt.router.oauth.errors import OAuthError
from metagpt.router.oauth.models import TokenClaims


class JWTDecodeError(OAuthError):
    """The token was not a well-formed JWT / payload could not be decoded."""


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url JWT segment, restoring missing padding."""
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as e:  # noqa: BLE001
        raise JWTDecodeError(f"invalid base64url segment: {e}") from e


def decode_jwt_payload(token: str) -> Dict:
    """Decode and return the JWT payload (claims) dict without verifying it.

    Raises :class:`JWTDecodeError` when the token is not a 3-part JWT or the
    payload is not valid JSON.
    """
    if not token or token.count(".") != 2:
        raise JWTDecodeError("not a JWT (expected three dot-separated segments)")
    _, payload_b64, _ = token.split(".")
    raw = _b64url_decode(payload_b64)
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise JWTDecodeError(f"payload is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise JWTDecodeError("payload is not a JSON object")
    return data


def parse_claims(token: str) -> TokenClaims:
    """Parse selected claims (email/account/exp) from a JWT access token.

    Returns a :class:`TokenClaims`; raises :class:`JWTDecodeError` on malformed
    input so callers can choose to ignore non-JWT (opaque) tokens.
    """
    data = decode_jwt_payload(token)
    exp = data.get("exp")
    return TokenClaims(
        email=data.get("email") or data.get("preferred_username"),
        account=data.get("account") or data.get("sub"),
        exp=int(exp) if isinstance(exp, (int, float)) else None,
        raw=data,
    )
