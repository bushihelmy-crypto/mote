#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentRegistry — session-scoped agent bookkeeping.

Port of ``codex-rs/core/src/agent/registry.rs``. The registry tracks the agent
tree (``path -> metadata``) and reserves nicknames (with an ordinal-suffix reset
pool when exhausted). Logical capacity is owned by the capacity projection,
not this identity index. ``ThreadId`` is replaced by ``Role.session_id`` (a plain
``str``); ``AgentPath`` keeps its identity.

This structure is shared by all agents in a session (it lives on ``AgentControl``).
Locking uses a plain ``threading.Lock`` mirroring the rust ``Mutex`` — all guarded
operations are short and synchronous.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterator, Optional

from mote.contracts.agent.errors import AgentNotKnown, AgentPathExists
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError, LeaseFencedError
from mote.orchestration.agents._scope import ScopedExitMixin
from mote.orchestration.agents.identity.path import AgentPath


@dataclass
class AgentMetadata:
    """Metadata for one agent in the tree (port of rust ``AgentMetadata``)."""

    agent_id: Optional[str] = None  # session_id
    agent_path: Optional[AgentPath] = None
    agent_nickname: Optional[str] = None
    agent_role: Optional[str] = None
    last_task_message: Optional[str] = None


@dataclass(frozen=True, slots=True)
class IdentityClaim:
    """Revisioned ownership of one path or nickname index value."""

    reservation_id: str
    revision: int
    agent_id: str | None = None
    tombstoned: bool = False


@dataclass(frozen=True, slots=True)
class IdentityReservationSnapshot:
    reservation_id: str
    path: str | None
    path_revision: int | None
    nickname: str | None
    nickname_revision: int | None

    def __post_init__(self) -> None:
        if type(self.reservation_id) is not str or not self.reservation_id:
            raise ValueError("identity reservation token is invalid")
        for identity, revision in (
            (self.path, self.path_revision),
            (self.nickname, self.nickname_revision),
        ):
            if (identity is None) != (revision is None):
                raise ValueError("identity reservation binding is incomplete")
            if identity is not None and (not identity or type(revision) is not int or revision < 1):
                raise ValueError("identity reservation binding is invalid")


class IdentityReclaimDisposition(StrEnum):
    RECLAIMED = "reclaimed"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"
    NOT_RECLAIMABLE = "not_reclaimable"
    STALE_FENCE = "stale_fence"
    OWNER_LOST = "owner_lost"


@dataclass(frozen=True, slots=True)
class IdentityReclaimReceipt:
    disposition: IdentityReclaimDisposition
    reservation_id: str


@dataclass(frozen=True, slots=True)
class IdentityRetentionRelease:
    agent_id: str
    path: str | None
    path_revision: int | None
    nickname: str | None
    nickname_revision: int | None

    def __post_init__(self) -> None:
        if type(self.agent_id) is not str or not self.agent_id:
            raise ValueError("identity retention release agent is invalid")
        for identity, revision in (
            (self.path, self.path_revision),
            (self.nickname, self.nickname_revision),
        ):
            if (identity is None) != (revision is None):
                raise ValueError("identity retention release binding is incomplete")
            if identity is not None and (not identity or type(revision) is not int or revision < 1):
                raise ValueError("identity retention release binding is invalid")
        if self.path is None and self.nickname is None:
            raise ValueError("identity retention release requires an index binding")


class AgentIndexKind(StrEnum):
    PATH = "path"
    NICKNAME = "nickname"


@dataclass(frozen=True, slots=True)
class AgentIndexReference:
    kind: AgentIndexKind
    value: str
    revision: int
    agent_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AgentIndexKind):
            raise TypeError("Agent index reference kind is invalid")
        for value in (self.value, self.agent_id):
            if type(value) is not str or not value:
                raise ValueError("Agent index reference identity is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Agent index reference revision is invalid")


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
    """Tracks the Agent identity tree and retained path/nickname claims."""

    def __init__(self):
        self._lock = threading.Lock()
        self._agent_tree: dict[str, AgentMetadata] = {}
        # Secondary index: agent_id -> the same AgentMetadata object held in
        # ``_agent_tree`` (which is keyed by path). Lets id-based lookups skip a
        # linear scan of the tree.
        self._by_agent_id: dict[str, AgentMetadata] = {}
        self._path_claims: dict[str, IdentityClaim] = {}
        self._nickname_claims: dict[str, IdentityClaim] = {}
        self._nickname_reset_count: int = 0
        self._next_identity_revision = 1
        self._active_reservations: set[str] = set()
        self._known_agent_ids: set[str] = set()

    @staticmethod
    def _tree_key(metadata: AgentMetadata) -> str:
        """The ``_agent_tree`` key for *metadata* (path str, or ``agent:<id>``)."""
        if metadata.agent_path is not None:
            return metadata.agent_path.as_str()
        return f"agent:{metadata.agent_id}"

    # ------------------------------------------------------------------
    # Spawn slot reservation
    # ------------------------------------------------------------------
    def reserve_spawn_identity(self) -> "SpawnReservation":
        """Reserve a unique identity transaction; this does not grant capacity."""
        with self._lock:
            reservation_id = uuid.uuid4().hex
            self._active_reservations.add(reservation_id)
        return SpawnReservation(self, reservation_id)

    def release_spawned_agent(self, agent_id: str) -> None:
        """Remove active membership while retaining tombstoned identity facts."""
        with self._lock:
            metadata = self._by_agent_id.pop(agent_id, None)
            if metadata is None:
                return
            self._agent_tree.pop(self._tree_key(metadata), None)
            self._tombstone_claim_locked(
                self._path_claims, metadata.agent_path.as_str() if metadata.agent_path else None
            )
            self._tombstone_claim_locked(self._nickname_claims, metadata.agent_nickname)

    # ------------------------------------------------------------------
    # Root / lookup
    # ------------------------------------------------------------------
    def register_root_agent(self, agent_id: str) -> None:
        with self._lock:
            if AgentPath.ROOT in self._agent_tree:
                current = self._agent_tree[AgentPath.ROOT]
                if current.agent_id != agent_id:
                    raise ValueError("root Agent identity conflicts with canonical root")
                return
            if agent_id in self._known_agent_ids:
                raise ValueError("logical Agent identity cannot be reused")
            metadata = AgentMetadata(agent_id=agent_id, agent_path=AgentPath.root())
            self._agent_tree[AgentPath.ROOT] = metadata
            self._by_agent_id[agent_id] = metadata
            self._path_claims[AgentPath.ROOT] = self._new_claim_locked("root", agent_id=agent_id)
            self._known_agent_ids.add(agent_id)

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

    def path_reference(self, agent_path: AgentPath) -> AgentIndexReference | None:
        with self._lock:
            claim = self._path_claims.get(agent_path.as_str())
            if claim is None or claim.agent_id is None or claim.tombstoned:
                return None
            return AgentIndexReference(
                AgentIndexKind.PATH,
                agent_path.as_str(),
                claim.revision,
                claim.agent_id,
            )

    def nickname_reference(self, nickname: str) -> AgentIndexReference | None:
        with self._lock:
            claim = self._nickname_claims.get(nickname)
            if claim is None or claim.agent_id is None or claim.tombstoned:
                return None
            return AgentIndexReference(
                AgentIndexKind.NICKNAME,
                nickname,
                claim.revision,
                claim.agent_id,
            )

    def resolve_index_reference(self, reference: AgentIndexReference) -> str | None:
        with self._lock:
            claims = self._path_claims if reference.kind is AgentIndexKind.PATH else self._nickname_claims
            claim = claims.get(reference.value)
            if (
                claim is None
                or claim.tombstoned
                or claim.revision != reference.revision
                or claim.agent_id != reference.agent_id
            ):
                return None
            return claim.agent_id

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
    def _release_pending_slot(self, reservation_id: str) -> bool:
        with self._lock:
            if reservation_id not in self._active_reservations:
                return False
            self._active_reservations.remove(reservation_id)
            return True

    def _new_claim_locked(self, reservation_id: str, *, agent_id: str | None = None) -> IdentityClaim:
        claim = IdentityClaim(reservation_id, self._next_identity_revision, agent_id)
        self._next_identity_revision += 1
        return claim

    def _tombstone_claim_locked(self, claims: dict[str, IdentityClaim], key: str | None) -> None:
        if key is None:
            return
        current = claims.get(key)
        if current is None or current.tombstoned:
            return
        claims[key] = IdentityClaim(
            reservation_id=current.reservation_id,
            revision=self._next_identity_revision,
            agent_id=current.agent_id,
            tombstoned=True,
        )
        self._next_identity_revision += 1

    def _register_spawned_agent(self, agent_metadata: AgentMetadata) -> None:
        if agent_metadata.agent_id is None:
            return
        with self._lock:
            reservation_id = f"direct:{agent_metadata.agent_id}"
            if agent_metadata.agent_id in self._known_agent_ids:
                raise ValueError("logical Agent identity cannot be reused")
            if agent_metadata.agent_path is not None:
                path = agent_metadata.agent_path.as_str()
                if path in self._path_claims:
                    raise AgentPathExists(path)
            if agent_metadata.agent_nickname:
                nickname = agent_metadata.agent_nickname
                if nickname in self._nickname_claims:
                    raise AgentNotKnown(message=f"agent nickname {nickname!r} is already claimed")
            if agent_metadata.agent_path is not None:
                path = agent_metadata.agent_path.as_str()
                self._path_claims[path] = self._new_claim_locked(reservation_id, agent_id=agent_metadata.agent_id)
            if agent_metadata.agent_nickname:
                nickname = agent_metadata.agent_nickname
                self._nickname_claims[nickname] = self._new_claim_locked(
                    reservation_id, agent_id=agent_metadata.agent_id
                )
            self._agent_tree[self._tree_key(agent_metadata)] = agent_metadata
            self._by_agent_id[agent_metadata.agent_id] = agent_metadata
            self._known_agent_ids.add(agent_metadata.agent_id)

    def register_spawned_agent(self, agent_metadata: AgentMetadata) -> None:
        """Register membership created outside a SpawnReservation."""
        self._register_spawned_agent(agent_metadata)

    def _reserve_agent_nickname(self, reservation_id: str, names: list[str], preferred: Optional[str]) -> Optional[str]:
        with self._lock:
            if preferred is not None:
                nickname = preferred
                if nickname in self._nickname_claims:
                    raise AgentNotKnown(message=f"agent nickname {nickname!r} is already claimed")
            else:
                if not names:
                    return None
                while True:
                    available = [
                        formatted
                        for name in names
                        if (formatted := format_agent_nickname(name, self._nickname_reset_count))
                        not in self._nickname_claims
                    ]
                    if available:
                        nickname = available[0]
                        break
                    self._nickname_reset_count += 1
            self._nickname_claims[nickname] = self._new_claim_locked(reservation_id)
            return nickname

    def _reserve_agent_path(self, reservation_id: str, agent_path: AgentPath) -> None:
        with self._lock:
            path = agent_path.as_str()
            if path in self._path_claims:
                raise AgentPathExists(path)
            self._path_claims[path] = self._new_claim_locked(reservation_id)

    def _release_reserved_claim(self, claims: dict[str, IdentityClaim], key: str, reservation_id: str) -> None:
        with self._lock:
            claim = claims.get(key)
            if claim is not None and claim.reservation_id == reservation_id and claim.agent_id is None:
                del claims[key]

    def _commit_reservation(
        self,
        reservation_id: str,
        agent_metadata: AgentMetadata,
        reserved_path: AgentPath | None,
        reserved_nickname: str | None,
    ) -> None:
        if agent_metadata.agent_id is None:
            raise ValueError("committed Agent metadata requires agent_id")
        if agent_metadata.agent_path != reserved_path:
            raise ValueError("committed Agent path does not match reservation")
        if agent_metadata.agent_nickname != reserved_nickname:
            raise ValueError("committed Agent nickname does not match reservation")
        with self._lock:
            if reservation_id not in self._active_reservations:
                raise RuntimeError("spawn reservation is no longer active")
            if agent_metadata.agent_id in self._known_agent_ids:
                raise ValueError("logical Agent identity cannot be reused")
            owned_claims = (
                (self._path_claims, reserved_path.as_str() if reserved_path else None),
                (self._nickname_claims, reserved_nickname),
            )
            for claims, key in owned_claims:
                if key is None:
                    continue
                claim = claims.get(key)
                if claim is None or claim.reservation_id != reservation_id or claim.agent_id is not None:
                    raise RuntimeError("spawn identity reservation ownership was lost")
            for claims, key in owned_claims:
                if key is None:
                    continue
                claims[key] = IdentityClaim(
                    reservation_id=reservation_id,
                    revision=self._next_identity_revision,
                    agent_id=agent_metadata.agent_id,
                )
                self._next_identity_revision += 1
            self._agent_tree[self._tree_key(agent_metadata)] = agent_metadata
            self._by_agent_id[agent_metadata.agent_id] = agent_metadata
            self._known_agent_ids.add(agent_metadata.agent_id)
            self._active_reservations.remove(reservation_id)

    def reclaim_aborted_reservation(
        self,
        snapshot: IdentityReservationSnapshot,
        *,
        lease: LeaseEpoch,
        coordinator: LeaseCoordinator,
    ) -> IdentityReclaimReceipt:
        with _identity_guard(coordinator, lease) as fenced:
            if fenced is not None:
                return IdentityReclaimReceipt(fenced, snapshot.reservation_id)
            with self._lock:
                if snapshot.reservation_id not in self._active_reservations:
                    return IdentityReclaimReceipt(IdentityReclaimDisposition.NOT_FOUND, snapshot.reservation_id)
                bindings = (
                    (self._path_claims, snapshot.path, snapshot.path_revision),
                    (self._nickname_claims, snapshot.nickname, snapshot.nickname_revision),
                )
                actual = {
                    ("path", identity, claim.revision)
                    for identity, claim in self._path_claims.items()
                    if claim.reservation_id == snapshot.reservation_id and claim.agent_id is None
                } | {
                    ("nickname", identity, claim.revision)
                    for identity, claim in self._nickname_claims.items()
                    if claim.reservation_id == snapshot.reservation_id and claim.agent_id is None
                }
                expected = {
                    (kind, identity, revision)
                    for kind, identity, revision in (
                        ("path", snapshot.path, snapshot.path_revision),
                        ("nickname", snapshot.nickname, snapshot.nickname_revision),
                    )
                    if identity is not None and revision is not None
                }
                if actual != expected:
                    return IdentityReclaimReceipt(
                        IdentityReclaimDisposition.REVISION_CONFLICT,
                        snapshot.reservation_id,
                    )
                for claims, identity, revision in bindings:
                    if identity is None:
                        continue
                    claim = claims.get(identity)
                    if claim is None or claim.reservation_id != snapshot.reservation_id or claim.revision != revision:
                        return IdentityReclaimReceipt(
                            IdentityReclaimDisposition.REVISION_CONFLICT,
                            snapshot.reservation_id,
                        )
                    if claim.agent_id is not None or claim.tombstoned:
                        return IdentityReclaimReceipt(
                            IdentityReclaimDisposition.NOT_RECLAIMABLE,
                            snapshot.reservation_id,
                        )
                for claims, identity, _ in bindings:
                    if identity is not None:
                        del claims[identity]
                self._active_reservations.remove(snapshot.reservation_id)
                return IdentityReclaimReceipt(IdentityReclaimDisposition.RECLAIMED, snapshot.reservation_id)

    def release_retained_indices(
        self,
        release: IdentityRetentionRelease,
        *,
        lease: LeaseEpoch,
        coordinator: LeaseCoordinator,
    ) -> IdentityReclaimReceipt:
        with _identity_guard(coordinator, lease) as fenced:
            if fenced is not None:
                return IdentityReclaimReceipt(fenced, release.agent_id)
            with self._lock:
                bindings = (
                    (self._path_claims, release.path, release.path_revision),
                    (self._nickname_claims, release.nickname, release.nickname_revision),
                )
                actual = {
                    ("path", identity, claim.revision)
                    for identity, claim in self._path_claims.items()
                    if claim.agent_id == release.agent_id
                } | {
                    ("nickname", identity, claim.revision)
                    for identity, claim in self._nickname_claims.items()
                    if claim.agent_id == release.agent_id
                }
                expected = {
                    (kind, identity, revision)
                    for kind, identity, revision in (
                        ("path", release.path, release.path_revision),
                        ("nickname", release.nickname, release.nickname_revision),
                    )
                    if identity is not None and revision is not None
                }
                if actual != expected:
                    return IdentityReclaimReceipt(IdentityReclaimDisposition.REVISION_CONFLICT, release.agent_id)
                for claims, identity, revision in bindings:
                    if identity is None:
                        continue
                    claim = claims.get(identity)
                    if claim is None or claim.agent_id != release.agent_id or claim.revision != revision:
                        return IdentityReclaimReceipt(IdentityReclaimDisposition.REVISION_CONFLICT, release.agent_id)
                    if not claim.tombstoned:
                        return IdentityReclaimReceipt(IdentityReclaimDisposition.NOT_RECLAIMABLE, release.agent_id)
                for claims, identity, _ in bindings:
                    if identity is not None:
                        del claims[identity]
                return IdentityReclaimReceipt(IdentityReclaimDisposition.RECLAIMED, release.agent_id)


class SpawnReservation(ScopedExitMixin):
    """A pending spawn slot. Commit to register the agent, else roll back.

    Supports explicit :meth:`commit` and the context-manager protocol (sync and
    async) — on an un-committed exit it releases any reserved path/nickname and
    decrements the total count, mirroring rust's ``Drop``.
    """

    def __init__(self, registry: AgentRegistry, reservation_id: str):
        self._registry = registry
        self.reservation_id = reservation_id
        self._active = True
        self._reserved_path: Optional[AgentPath] = None
        self._reserved_nickname: str | None = None

    def reserve_agent_nickname_with_preference(self, names: list[str], preferred: Optional[str] = None) -> str:
        if self._reserved_nickname is not None:
            raise RuntimeError("reservation already owns a nickname")
        nickname = self._registry._reserve_agent_nickname(self.reservation_id, names, preferred)
        if nickname is None:
            raise AgentNotKnown(message="no available agent nicknames")
        self._reserved_nickname = nickname
        return nickname

    def reserve_agent_path(self, agent_path: AgentPath) -> None:
        if self._reserved_path is not None:
            raise RuntimeError("reservation already owns a path")
        self._registry._reserve_agent_path(self.reservation_id, agent_path)
        self._reserved_path = agent_path

    def commit(self, agent_metadata: AgentMetadata) -> None:
        self._registry._commit_reservation(
            self.reservation_id,
            agent_metadata,
            self._reserved_path,
            self._reserved_nickname,
        )
        self._active = False

    def snapshot(self) -> IdentityReservationSnapshot:
        path = self._reserved_path.as_str() if self._reserved_path is not None else None
        with self._registry._lock:
            path_claim = self._registry._path_claims.get(path) if path is not None else None
            nickname_claim = (
                self._registry._nickname_claims.get(self._reserved_nickname)
                if self._reserved_nickname is not None
                else None
            )
            return IdentityReservationSnapshot(
                reservation_id=self.reservation_id,
                path=path,
                path_revision=path_claim.revision if path_claim is not None else None,
                nickname=self._reserved_nickname,
                nickname_revision=nickname_claim.revision if nickname_claim is not None else None,
            )

    def rollback(self) -> None:
        if not self._active:
            return
        if self._reserved_path is not None:
            self._registry._release_reserved_claim(
                self._registry._path_claims,
                self._reserved_path.as_str(),
                self.reservation_id,
            )
            self._reserved_path = None
        if self._reserved_nickname is not None:
            self._registry._release_reserved_claim(
                self._registry._nickname_claims,
                self._reserved_nickname,
                self.reservation_id,
            )
            self._reserved_nickname = None
        self._registry._release_pending_slot(self.reservation_id)
        self._active = False

    def _scope_exit(self) -> None:
        self.rollback()


@contextmanager
def _identity_guard(coordinator: LeaseCoordinator, lease: LeaseEpoch) -> Iterator[IdentityReclaimDisposition | None]:
    guard = coordinator.guard(lease.subject, lease.fencing_token)
    try:
        guard.__enter__()
    except LeaseFencedError:
        yield IdentityReclaimDisposition.STALE_FENCE
        return
    except LeaseCoordinatorUnavailableError:
        yield IdentityReclaimDisposition.OWNER_LOST
        return
    try:
        yield None
    finally:
        guard.__exit__(None, None, None)


__all__ = [
    "AgentMetadata",
    "AgentIndexKind",
    "AgentIndexReference",
    "AgentRegistry",
    "IdentityClaim",
    "IdentityReclaimDisposition",
    "IdentityReclaimReceipt",
    "IdentityReservationSnapshot",
    "IdentityRetentionRelease",
    "SpawnReservation",
    "format_agent_nickname",
    "exceeds_agent_spawn_depth_limit",
    "next_agent_spawn_depth",
]
