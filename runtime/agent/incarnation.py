"""In-process Agent incarnation blueprints for Residency replacement."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from mote.contracts.content import ContentDigest
from mote.contracts.events.envelope import JsonValue
from mote.runtime.agent.base import BaseRole

AgentSnapshot = Mapping[str, JsonValue]
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

    definition_id: str
    config_digest: ContentDigest
    restore: AgentRestorer

    def build(self, state: AgentSnapshot) -> BaseRole:
        restored = self.restore(state)
        if restored.residency_definition_id != self.definition_id:
            raise AgentIncarnationError("incarnation factory restored another Agent definition")
        if restored.residency_config_digest != self.config_digest:
            raise AgentIncarnationError("incarnation factory restored another Agent configuration")
        return restored


class AgentIncarnationFactory:
    """Thread-safe owner of per-session in-process replacement blueprints."""

    def __init__(self) -> None:
        self._blueprints: dict[str, AgentIncarnationBlueprint] = {}
        self._generations: dict[str, int] = {}
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
            self._generations.setdefault(normalized, 1)

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

    def factory(self, session_id: str) -> AgentIncarnationBlueprint:
        with self._lock:
            blueprint = self._blueprints.get(session_id)
        if blueprint is None:
            raise AgentIncarnationError(f"no trusted Agent incarnation blueprint for session {session_id!r}")
        return blueprint

    def generation(self, session_id: str) -> int:
        with self._lock:
            generation = self._generations.get(session_id)
        if generation is None:
            raise AgentIncarnationError(f"no Agent incarnation generation for session {session_id!r}")
        return generation

    def advance_generation(self, session_id: str, *, expected: int) -> int:
        with self._lock:
            current = self._generations.get(session_id)
            if current != expected:
                raise AgentIncarnationError("Agent incarnation generation changed concurrently")
            next_generation = expected + 1
            self._generations[session_id] = next_generation
            return next_generation

    def rollback_generation(self, session_id: str, *, expected_current: int, restore: int) -> None:
        with self._lock:
            if expected_current != restore + 1 or self._generations.get(session_id) != expected_current:
                raise AgentIncarnationError("Agent incarnation generation rollback lost ownership")
            self._generations[session_id] = restore

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._blueprints.pop(session_id, None)
            self._generations.pop(session_id, None)

    def has(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._blueprints


__all__ = [
    "AgentIncarnationBlueprint",
    "AgentIncarnationError",
    "AgentIncarnationFactory",
]
