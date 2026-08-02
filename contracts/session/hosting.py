"""Typed remote Session hosting failures."""

from __future__ import annotations

from enum import Enum


class SessionHostingErrorKind(str, Enum):
    NOT_FOUND = "not_found"
    LOAD_FAILED = "load_failed"
    FORK_UNSUPPORTED = "fork_unsupported"
    FORK_FAILED = "fork_failed"


class SessionHostingError(RuntimeError):
    def __init__(self, kind: SessionHostingErrorKind, session_id: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.session_id = session_id


__all__ = ["SessionHostingError", "SessionHostingErrorKind"]
