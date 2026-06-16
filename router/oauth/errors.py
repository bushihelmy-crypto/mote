#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuth error hierarchy + refresh-failure classification.

The classifier maps an OAuth2 error-response ``error`` code (RFC 6749 §5.2 /
RFC 6750) to a typed exception so the manager/LLM layer can decide whether a
refresh is permanently broken (re-auth required) or merely transient.
"""
from __future__ import annotations

from typing import Optional

# Exception types are unified under the global exception package; re-exported
# here so existing ``from metagpt.router.oauth.errors import OAuthError`` call
# sites keep working. Only the refresh-failure classification (OAuth-domain
# logic) lives in this module.
from metagpt.common.exception import (  # noqa: F401
    OAuthConfigError,
    OAuthError,
    OAuthHTTPError,
    OAuthRefreshError,
)

# OAuth2 error codes that mean the refresh token is permanently unusable.
_UNRECOVERABLE_ERROR_CODES = {
    "invalid_grant",  # expired / revoked / reused refresh token
    "invalid_client",  # client credentials rejected
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
}


def classify_refresh_failure(
    *,
    status_code: Optional[int] = None,
    error_code: Optional[str] = None,
    description: Optional[str] = None,
) -> OAuthRefreshError:
    """Build an :class:`OAuthRefreshError` classified by OAuth2 ``error`` code.

    Unrecoverable codes (``invalid_grant`` family) => ``recoverable=False``.
    Server errors (5xx) or no error code => treated as transient (recoverable).
    """
    code = (error_code or "").strip().lower()
    msg = description or error_code or f"OAuth refresh failed (status={status_code})"

    if code in _UNRECOVERABLE_ERROR_CODES:
        return OAuthRefreshError(msg, error_code=code, recoverable=False, status_code=status_code)

    # 5xx and transport-ish failures are retryable; everything else with an
    # unknown 4xx is conservatively non-recoverable.
    recoverable = status_code is None or status_code >= 500 or not code
    return OAuthRefreshError(msg, error_code=code or None, recoverable=recoverable, status_code=status_code)
