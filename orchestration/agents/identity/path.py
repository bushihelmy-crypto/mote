#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentPath — hierarchical routing key for the multi-agent control plane.

Faithful Python port of ``codex-rs/protocol/src/agent_path.rs``. An ``AgentPath``
is an absolute, validated path like ``/root`` or ``/root/researcher/worker`` that
identifies an agent in the session tree. It is an *independent routing key*: the
registry maps path <-> ``session_id``, so the path is never the on-disk identity.

Pure leaf module — zero dependencies on the rest of the environment package.
"""

from __future__ import annotations

from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

ROOT = "/root"
MORPHEUS = "/morpheus"
ROOT_SEGMENT = "root"


class AgentPath:
    """An absolute, validated agent path (value object)."""

    __slots__ = ("_value",)

    # Mirror the rust associated constants for callers that reach for them.
    ROOT = ROOT
    MORPHEUS = MORPHEUS

    def __init__(self, value: str):
        _validate_absolute_path(value)
        self._value = value

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------
    @classmethod
    def root(cls) -> "AgentPath":
        return cls(ROOT)

    @classmethod
    def morpheus(cls) -> "AgentPath":
        return cls(MORPHEUS)

    @classmethod
    def from_string(cls, path: str) -> "AgentPath":
        return cls(path)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def as_str(self) -> str:
        return self._value

    def is_root(self) -> bool:
        return self._value == ROOT

    def name(self) -> str:
        """The last path segment; ``root`` for the root path."""
        if self.is_root():
            return ROOT_SEGMENT
        segment = self._value.rsplit("/", 1)[-1]
        return segment if segment else ROOT_SEGMENT

    def parent(self) -> "AgentPath | None":
        """The parent path, or ``None`` for the root / a single-segment path."""
        if self.is_root() or self._value == MORPHEUS:
            return None
        parent, _, _ = self._value.rpartition("/")
        if not parent:
            return None
        try:
            return AgentPath(parent)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------
    def join(self, agent_name: str) -> "AgentPath":
        _validate_agent_name(agent_name)
        return AgentPath(f"{self._value}/{agent_name}")

    def resolve(self, reference: str) -> "AgentPath":
        """Resolve *reference* (relative or absolute) against this path."""
        if not reference:
            raise ValueError("agent path must not be empty")
        if reference == ROOT:
            return AgentPath.root()
        if reference.startswith("/"):
            return AgentPath(reference)
        _validate_relative_reference(reference)
        return AgentPath(f"{self._value}/{reference}")

    # ------------------------------------------------------------------
    # Dunders
    # ------------------------------------------------------------------
    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"AgentPath({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AgentPath):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __lt__(self, other: "AgentPath") -> bool:
        return self._value < other._value

    # ------------------------------------------------------------------
    # Pydantic integration — validate str -> AgentPath, serialize -> str.
    # ------------------------------------------------------------------
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler):
        return core_schema.no_info_plain_validator_function(
            cls._pydantic_validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: v.as_str(), return_schema=core_schema.str_schema()
            ),
        )

    @classmethod
    def _pydantic_validate(cls, value: Any) -> "AgentPath":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls.from_string(value)
        raise ValueError(f"cannot build AgentPath from {type(value).__name__}")


# ----------------------------------------------------------------------
# Validators (ported from agent_path.rs)
# ----------------------------------------------------------------------
def _validate_agent_name(agent_name: str) -> None:
    if not agent_name:
        raise ValueError("agent_name must not be empty")
    if agent_name == ROOT_SEGMENT:
        raise ValueError("agent_name `root` is reserved")
    if agent_name in (".", ".."):
        raise ValueError(f"agent_name `{agent_name}` is reserved")
    if "/" in agent_name:
        raise ValueError("agent_name must not contain `/`")
    if not all(("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_" for ch in agent_name):
        raise ValueError("agent_name must use only lowercase letters, digits, and underscores")


def _validate_absolute_path(path: str) -> None:
    if path == MORPHEUS:
        return
    if not path.startswith("/"):
        raise ValueError("absolute agent paths must start with `/root` or be `/morpheus`")
    stripped = path[1:]
    segments = stripped.split("/")
    root = segments[0]
    if root != ROOT_SEGMENT:
        raise ValueError("absolute agent paths must start with `/root` or be `/morpheus`")
    if stripped.endswith("/"):
        raise ValueError("absolute agent path must not end with `/`")
    for segment in segments[1:]:
        _validate_agent_name(segment)


def _validate_relative_reference(reference: str) -> None:
    if reference.endswith("/"):
        raise ValueError("relative agent path must not end with `/`")
    for segment in reference.split("/"):
        _validate_agent_name(segment)


__all__ = ["AgentPath", "ROOT", "MORPHEUS", "ROOT_SEGMENT"]
