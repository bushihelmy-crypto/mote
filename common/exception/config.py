"""Config / environment tier exceptions.

Pydantic only collects exceptions raised inside ``@field_validator`` into a
``ValidationError`` when they are ``ValueError`` (or ``AssertionError``)
subclasses. ``ConfigValidationError`` therefore multiply-inherits ``ValueError``
so config validators can raise typed errors and still surface as a single
``ValidationError`` instead of crashing the model construction.
"""

from __future__ import annotations

from typing import ClassVar

from metagpt.common.exception.base import MetaGPTError
from metagpt.common.exception.codes import ErrorCode


class ConfigError(MetaGPTError):
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


class EnvKeyNotFoundError(ConfigError):
    """An environment variable / RFC-216 key was not found.

    Preserves the historical ``__init__(self, info)`` signature so existing
    ``raise EnvKeyNotFoundError(...)`` and ``except EnvKeyNotFoundError`` call
    sites (e.g. ``get_env_default``) keep working unchanged.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ENV_KEY_NOT_FOUND

    def __init__(self, info: str = "") -> None:
        super().__init__(info)
