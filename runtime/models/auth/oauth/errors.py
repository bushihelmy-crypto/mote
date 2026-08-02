#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuth error hierarchy + refresh-failure classification.

The classifier maps an OAuth2 error-response ``error`` code (RFC 6749 §5.2 /
RFC 6750) to a typed exception so the manager/LLM layer can decide whether a
refresh is permanently broken (re-auth required) or merely transient.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from mote.contracts.foundation.errors.base import MoteError, NonRetryableError
from mote.contracts.foundation.errors.codes import ErrorCode


class OAuthError(MoteError):
    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH


class OAuthConfigError(OAuthError, NonRetryableError):
    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_CONFIG


class OAuthHTTPError(OAuthError):
    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_HTTP

    def __init__(self, message: str, status_code: int | None = None, error_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class OAuthRefreshError(OAuthError):
    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_REFRESH

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        recoverable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.recoverable = recoverable
        self.status_code = status_code


class JWTDecodeError(OAuthError):
    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_JWT


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
