#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentRegistry — session-scoped agent bookkeeping.

Port of ``codex-rs/core/src/agent/registry.rs``. The registry tracks the agent
tree (``path -> metadata``), reserves nicknames (with an ordinal-suffix reset
pool when exhausted), and enforces the total agent count per session via
``reserve_spawn_slot``. ``ThreadId`` is replaced by ``Role.session_id`` (a plain
``str``); ``AgentPath`` keeps its identity.

This structure is shared by all agents in a session (it lives on ``AgentControl``).
Locking uses a plain ``threading.Lock`` mirroring the rust ``Mutex`` — all guarded
operations are short and synchronous.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from typing import Optional

from mote.common.exception import AgentLimitReached, AgentNotKnown, AgentPathExists
from mote.environment._scope import ScopedExitMixin
from mote.environment.agent_path import AgentPath


@dataclass
class AgentMetadata:
    """Metadata for one agent in the tree (port of rust ``AgentMetadata``)."""

    agent_id: Optional[str] = None  # session_id
    agent_path: Optional[AgentPath] = None
    agent_nickname: Optional[str] = None
    agent_role: Optional[str] = None
    last_task_message: Optional[str] = None


# ----------------------------------------------------------------------
# Nickname / depth helpers (free functions, ported verbatim)
# ----------------------------------------------------------------------
def format_agent_nickname(name: str, nickname_reset_count: int) -> str:
    if nickname_reset_count == 0:
        return name
    value = nickname_reset_count + 1
    if 11 <= value % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{name} the {value}{suffix}"


def next_agent_spawn_depth(depth: int) -> int:
    """The depth of a child spawned from a parent at *depth*."""
    return depth + 1


def exceeds_agent_spawn_depth_limit(depth: int, max_depth: int) -> bool:
    return depth > max_depth


class AgentRegistry:
    """Tracks the agent tree, nickname pool, and total agent count."""

    def __init__(self):
        self._lock = threading.Lock()
        self._agent_tree: dict[str, AgentMetadata] = {}
        # Secondary index: agent_id -> the same AgentMetadata object held in
        # ``_agent_tree`` (which is keyed by path). Lets id-based lookups skip a
        # linear scan of the tree.
        self._by_agent_id: dict[str, AgentMetadata] = {}
        self._used_agent_nicknames: set[str] = set()
        self._nickname_reset_count: int = 0
        self._total_count: int = 0

    @staticmethod
    def _tree_key(metadata: AgentMetadata) -> str:
        """The ``_agent_tree`` key for *metadata* (path str, or ``agent:<id>``)."""
        if metadata.agent_path is not None:
            return metadata.agent_path.as_str()
        return f"agent:{metadata.agent_id}"

    # ------------------------------------------------------------------
    # Spawn slot reservation
    # ------------------------------------------------------------------
    def reserve_spawn_slot(self, max_agents: Optional[int] = None) -> "SpawnReservation":
        """Reserve a slot, enforcing *max_agents* when given.

        ``total_count`` is always incremented (so the matching rollback /
        ``release_spawned_agent`` always decrements). Raises
        :class:`AgentLimitReached` when the cap would be exceeded.
        """
        with self._lock:
            if max_agents is not None and self._total_count >= max_agents:
                raise AgentLimitReached(max_agents)
            self._total_count += 1
        return SpawnReservation(self)

    def release_spawned_agent(self, agent_id: str) -> None:
        """Remove a committed agent and decrement the count (non-root only)."""
        with self._lock:
            metadata = self._by_agent_id.pop(agent_id, None)
            if metadata is None:
                return
            self._agent_tree.pop(self._tree_key(metadata), None)
            is_root = metadata.agent_path is not None and metadata.agent_path.is_root()
            if not is_root:
                self._total_count = max(0, self._total_count - 1)

    # ------------------------------------------------------------------
    # Root / lookup
    # ------------------------------------------------------------------
    def register_root_agent(self, agent_id: str) -> None:
        with self._lock:
            if AgentPath.ROOT in self._agent_tree:
                return
            metadata = AgentMetadata(agent_id=agent_id, agent_path=AgentPath.root())
            self._agent_tree[AgentPath.ROOT] = metadata
            self._by_agent_id[agent_id] = metadata

    def agent_id_for_path(self, agent_path: AgentPath) -> Optional[str]:
        with self._lock:
            metadata = self._agent_tree.get(agent_path.as_str())
            return metadata.agent_id if metadata else None

    def agent_metadata_for_id(self, agent_id: str) -> Optional[AgentMetadata]:
        with self._lock:
            return self._by_agent_id.get(agent_id)

    def agent_metadata_for_nickname(self, nickname: str) -> Optional[AgentMetadata]:
        with self._lock:
            for metadata in self._agent_tree.values():
                if metadata.agent_nickname == nickname and metadata.agent_id is not None:
                    return metadata
            return None

    def live_agents(self) -> list[AgentMetadata]:
        with self._lock:
            return [
                metadata
                for metadata in self._agent_tree.values()
                if metadata.agent_id is not None
                and not (metadata.agent_path is not None and metadata.agent_path.is_root())
            ]

    def update_last_task_message(self, agent_id: str, last_task_message: str) -> None:
        with self._lock:
            metadata = self._by_agent_id.get(agent_id)
            if metadata is not None:
                metadata.last_task_message = last_task_message

    def clear_last_task_message(self, agent_id: str) -> None:
        with self._lock:
            metadata = self._by_agent_id.get(agent_id)
            if metadata is not None:
                metadata.last_task_message = None

    # ------------------------------------------------------------------
    # Internal helpers used by SpawnReservation
    # ------------------------------------------------------------------
    def _decrement_total(self) -> None:
        with self._lock:
            self._total_count = max(0, self._total_count - 1)

    def _register_spawned_agent(self, agent_metadata: AgentMetadata) -> None:
        if agent_metadata.agent_id is None:
            return
        with self._lock:
            if agent_metadata.agent_nickname:
                self._used_agent_nicknames.add(agent_metadata.agent_nickname)
            self._agent_tree[self._tree_key(agent_metadata)] = agent_metadata
            self._by_agent_id[agent_metadata.agent_id] = agent_metadata

    def _reserve_agent_nickname(self, names: list[str], preferred: Optional[str]) -> Optional[str]:
        with self._lock:
            if preferred is not None:
                nickname = preferred
            else:
                if not names:
                    return None
                available = [
                    formatted
                    for name in names
                    if (formatted := format_agent_nickname(name, self._nickname_reset_count))
                    not in self._used_agent_nicknames
                ]
                if available:
                    nickname = random.choice(available)
                else:
                    self._used_agent_nicknames.clear()
                    self._nickname_reset_count += 1
                    nickname = format_agent_nickname(random.choice(names), self._nickname_reset_count)
            self._used_agent_nicknames.add(nickname)
            return nickname

    def _reserve_agent_path(self, agent_path: AgentPath) -> None:
        with self._lock:
            if agent_path.as_str() in self._agent_tree:
                raise AgentPathExists(agent_path.as_str())
            self._agent_tree[agent_path.as_str()] = AgentMetadata(agent_path=agent_path)

    def _release_reserved_agent_path(self, agent_path: AgentPath) -> None:
        with self._lock:
            metadata = self._agent_tree.get(agent_path.as_str())
            if metadata is not None and metadata.agent_id is None:
                del self._agent_tree[agent_path.as_str()]


class SpawnReservation(ScopedExitMixin):
    """A pending spawn slot. Commit to register the agent, else roll back.

    Supports explicit :meth:`commit` and the context-manager protocol (sync and
    async) — on an un-committed exit it releases any reserved path/nickname and
    decrements the total count, mirroring rust's ``Drop``.
    """

    def __init__(self, registry: AgentRegistry):
        self._registry = registry
        self._active = True
        self._reserved_path: Optional[AgentPath] = None

    def reserve_agent_nickname_with_preference(self, names: list[str], preferred: Optional[str] = None) -> str:
        nickname = self._registry._reserve_agent_nickname(names, preferred)
        if nickname is None:
            raise AgentNotKnown(message="no available agent nicknames")
        return nickname

    def reserve_agent_path(self, agent_path: AgentPath) -> None:
        self._registry._reserve_agent_path(agent_path)
        self._reserved_path = agent_path

    def commit(self, agent_metadata: AgentMetadata) -> None:
        self._reserved_path = None
        self._registry._register_spawned_agent(agent_metadata)
        self._active = False

    def rollback(self) -> None:
        if not self._active:
            return
        if self._reserved_path is not None:
            self._registry._release_reserved_agent_path(self._reserved_path)
            self._reserved_path = None
        self._registry._decrement_total()
        self._active = False

    def _scope_exit(self) -> None:
        self.rollback()


__all__ = [
    "AgentMetadata",
    "AgentRegistry",
    "SpawnReservation",
    "format_agent_nickname",
    "next_agent_spawn_depth",
    "exceeds_agent_spawn_depth_limit",
]
