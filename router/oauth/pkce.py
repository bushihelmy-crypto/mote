#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PKCE (RFC 7636) + state helpers.

Added in P1 (cheap, unit-tested) so the P2 interactive auth-code flow has no new
primitives to build. Not used by the P1 headless grants.
"""
from __future__ import annotations

import base64
import hashlib
import secrets


def _b64url_no_pad(data: bytes) -> str:
    """Base64url-encode without trailing ``=`` padding (RFC 7636 §A)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def gen_code_verifier(n_bytes: int = 32) -> str:
    """Generate a high-entropy PKCE ``code_verifier``.

    RFC 7636 requires 43-128 chars from the unreserved set; base64url of 32-96
    random bytes satisfies this. Default 32 bytes -> 43 chars.
    """
    if not 32 <= n_bytes <= 96:
        raise ValueError("n_bytes must be between 32 and 96 to satisfy RFC 7636 length")
    return _b64url_no_pad(secrets.token_bytes(n_bytes))


def gen_code_challenge(verifier: str) -> str:
    """Compute the S256 ``code_challenge`` = base64url(SHA256(verifier))."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url_no_pad(digest)


def gen_state(n_bytes: int = 16) -> str:
    """Generate an opaque anti-CSRF ``state`` value."""
    return _b64url_no_pad(secrets.token_bytes(n_bytes))
