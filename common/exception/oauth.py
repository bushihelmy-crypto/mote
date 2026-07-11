"""OAuth / credential tier exceptions (``mote.router.oauth``).

The OAuth subsystem talks to a token endpoint and classifies refresh failures as
permanently broken (re-auth required) or transient. The refresh-failure
classification logic (``classify_refresh_failure`` + the unrecoverable error-code
set) stays in ``mote.router.oauth.errors`` as OAuth-domain logic; only the
exception *types* are unified here.

``OAuthRefreshError.recoverable`` is a *per-instance* flag decided at runtime by
the classifier from the provider's OAuth2 ``error`` code, so it is kept as an
instance attribute rather than folded into the class-level ``retryable`` marker.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from mote.common.exception.base import MoteError, NonRetryableError
from mote.common.exception.codes import ErrorCode


class OAuthError(MoteError):
    """Base for all OAuth failures."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH


class OAuthConfigError(OAuthError, NonRetryableError):
    """The OAuth provider config is missing/invalid for the requested operation."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_CONFIG


class OAuthHTTPError(OAuthError):
    """Token endpoint returned a non-2xx response not otherwise classified."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_HTTP

    def __init__(self, message: str, status_code: Optional[int] = None, error_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class OAuthRefreshError(OAuthError):
    """Refreshing (or minting) a token failed.

    ``error_code`` carries the provider's OAuth2 ``error`` field when present.
    ``recoverable`` is True for transient failures (retry/re-mint may help) and
    False when the grant is permanently broken (interactive re-auth required).
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_REFRESH

    def __init__(
        self,
        message: str,
        *,
        error_code: Optional[str] = None,
        recoverable: bool = False,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.recoverable = recoverable
        self.status_code = status_code


class JWTDecodeError(OAuthError):
    """The token was not a well-formed JWT / payload could not be decoded."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OAUTH_JWT
