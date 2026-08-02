"""Stable configuration validation errors.

Pydantic only collects exceptions raised inside ``@field_validator`` into a
``ValidationError`` when they are ``ValueError`` (or ``AssertionError``)
subclasses. ``ConfigValidationError`` therefore multiply-inherits ``ValueError``
so config validators can raise typed errors and still surface as a single
``ValidationError`` instead of crashing the model construction.
"""

from __future__ import annotations

from typing import ClassVar, List

from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.foundation.errors.codes import ErrorCode


class ConfigError(MoteError):
    """Base for configuration / environment failures."""


class ConfigValidationError(ConfigError, ValueError):
    """A configuration value failed validation.

    Inherits ``ValueError`` so pydantic ``@field_validator`` raises are
    collected into ``ValidationError`` rather than propagating raw.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.CONFIG_INVALID


class MissingAPIKeyError(ConfigValidationError):
    """A required API key was not configured."""

    default_code: ClassVar[ErrorCode] = ErrorCode.CONFIG_MISSING_API_KEY


class UnknownConfigKeysError(ConfigValidationError):
    """Strict mode: the merged config carries keys no field accepts.

    Subclasses :class:`ConfigValidationError` so existing
    ``except ConfigValidationError`` handlers still catch it; carries the list of
    offending dotted ``unknown_paths`` for diagnostics.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.CONFIG_UNKNOWN_KEYS

    def __init__(self, unknown_paths: List[str]) -> None:
        self.unknown_paths = list(unknown_paths)
        joined = ", ".join(self.unknown_paths)
        super().__init__(f"Unknown config keys (strict mode): {joined}")


class ConfigSourceChangedError(ConfigError):
    """A discovered configuration source changed before its bytes were read."""

    default_code: ClassVar[ErrorCode] = ErrorCode.CONFIG_INVALID


class EnvKeyNotFoundError(ConfigError):
    """An environment variable / RFC-216 key was not found.

    Preserves the historical ``__init__(self, info)`` signature so existing
    ``raise EnvKeyNotFoundError(...)`` and ``except EnvKeyNotFoundError`` call
    sites (e.g. ``get_env_default``) keep working unchanged.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ENV_KEY_NOT_FOUND

    def __init__(self, info: str = "") -> None:
        super().__init__(info)
