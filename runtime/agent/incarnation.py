"""In-process Agent incarnation blueprints for Residency replacement."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mote.runtime.agent.base import BaseRole

AgentSnapshot = Mapping[str, Any]
AgentRestorer = Callable[[AgentSnapshot], BaseRole]


class AgentIncarnationError(RuntimeError):
    """An Agent replacement cannot be constructed from its registered blueprint."""


@dataclass(frozen=True, slots=True)
class AgentIncarnationBlueprint:
    """Immutable recipe for replacing one evicted in-process Agent.

    The callable captures only construction values such as the concrete class,
    immutable wiring, and configuration. It must not capture live Role state;
    state comes exclusively from the Residency snapshot and rollout.
    """

    role_type: str
    snapshot_type_id: str | None
    restore: AgentRestorer

    def build(self, snapshot: AgentSnapshot) -> BaseRole:
        recorded_type_id = snapshot.get("type_id")
        if recorded_type_id != self.snapshot_type_id:
            raise AgentIncarnationError(
                f"incarnation snapshot type {recorded_type_id!r} does not match "
                f"blueprint type {self.snapshot_type_id!r}"
            )
        restored = self.restore(snapshot)
        actual = f"{type(restored).__module__}.{type(restored).__qualname__}"
        if actual != self.role_type:
            raise AgentIncarnationError(f"incarnation blueprint for {self.role_type!r} restored {actual!r}")
        return restored


class AgentIncarnationFactory:
    """Thread-safe owner of per-session in-process replacement blueprints."""

    def __init__(self) -> None:
        self._blueprints: dict[str, AgentIncarnationBlueprint] = {}
        self._lock = threading.RLock()

    def register(self, session_id: str, blueprint: AgentIncarnationBlueprint) -> None:
        normalized = session_id.strip()
        if not normalized:
            raise AgentIncarnationError("Agent incarnation session id must not be empty")
        with self._lock:
            existing = self._blueprints.get(normalized)
            if existing is not None and existing != blueprint:
                raise AgentIncarnationError(f"Agent incarnation blueprint already registered for {normalized!r}")
            self._blueprints[normalized] = blueprint

    def restore(self, session_id: str, snapshot: AgentSnapshot) -> BaseRole:
        with self._lock:
            blueprint = self._blueprints.get(session_id)
        if blueprint is None:
            raise AgentIncarnationError(
                f"no in-process Agent incarnation blueprint for session {session_id!r}; "
                "Residency snapshots are not cross-process construction records"
            )
        return blueprint.build(snapshot)

    def loader(self, session_id: str) -> AgentRestorer:
        """Return a store-compatible loader without exposing the registry map."""

        def load(snapshot: AgentSnapshot) -> BaseRole:
            return self.restore(session_id, snapshot)

        return load

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._blueprints.pop(session_id, None)

    def has(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._blueprints


__all__ = [
    "AgentIncarnationBlueprint",
    "AgentIncarnationError",
    "AgentIncarnationFactory",
]
