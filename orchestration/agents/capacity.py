"""Canonical logical-Agent capacity projection."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from mote.contracts.agent.capacity import (
    CapacityReservationDisposition,
    CapacitySettlementDisposition,
    LogicalCapacityLimit,
    LogicalCapacityReservationReceipt,
    LogicalCapacityScope,
    LogicalCapacityScopeKind,
    LogicalCapacitySettlementReceipt,
)
from mote.runtime.persistence.atomic import atomic_write


@dataclass(frozen=True, slots=True)
class LogicalCapacityFact:
    revision: int
    reservation_id: str
    scopes: tuple[LogicalCapacityScope, ...]
    active: bool


class LogicalCapacityProjection:
    """Revisioned, strictly-once logical admission and settlement projection."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._revision = 0
        self._counts: dict[LogicalCapacityScope, int] = {}
        self._active: dict[str, LogicalCapacityFact] = {}
        self._settled: set[str] = set()
        self._facts: list[LogicalCapacityFact] = []
        if path is not None:
            restored = self._decode(path)
            self._adopt(restored)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def reserve(
        self, limits: tuple[LogicalCapacityLimit, ...], *, expected_revision: int, reservation_id: str | None = None
    ) -> LogicalCapacityReservationReceipt:
        scopes = tuple(limit.scope for limit in limits)
        if len(set(scopes)) != len(scopes):
            raise ValueError("logical capacity limits contain duplicate scopes")
        identity = reservation_id or uuid.uuid4().hex
        with self._lock:
            if expected_revision != self._revision:
                return LogicalCapacityReservationReceipt(
                    identity, self._revision, scopes, CapacityReservationDisposition.REVISION_CONFLICT
                )
            existing = self._active.get(identity)
            if existing is not None:
                return LogicalCapacityReservationReceipt(
                    identity, self._revision, existing.scopes, CapacityReservationDisposition.RESERVED
                )
            if identity in self._settled:
                return LogicalCapacityReservationReceipt(
                    identity, self._revision, scopes, CapacityReservationDisposition.REVISION_CONFLICT
                )
            if any(self._counts.get(limit.scope, 0) >= limit.maximum for limit in limits):
                return LogicalCapacityReservationReceipt(
                    identity, self._revision, scopes, CapacityReservationDisposition.REJECTED_CAPACITY
                )
            prior = tuple(self._facts)
            self._revision += 1
            fact = LogicalCapacityFact(self._revision, identity, scopes, True)
            self._active[identity] = fact
            for scope in scopes:
                self._counts[scope] = self._counts.get(scope, 0) + 1
            self._facts.append(fact)
            self._persist_or_restore(prior)
            return LogicalCapacityReservationReceipt(
                identity, self._revision, scopes, CapacityReservationDisposition.RESERVED
            )

    def settle(self, reservation_id: str, *, expected_revision: int) -> LogicalCapacitySettlementReceipt:
        with self._lock:
            if expected_revision != self._revision:
                return LogicalCapacitySettlementReceipt(
                    reservation_id, self._revision, CapacitySettlementDisposition.REVISION_CONFLICT
                )
            prior = tuple(self._facts)
            fact = self._active.pop(reservation_id, None)
            if fact is None:
                disposition = (
                    CapacitySettlementDisposition.ALREADY_SETTLED
                    if reservation_id in self._settled
                    else CapacitySettlementDisposition.NOT_FOUND
                )
                return LogicalCapacitySettlementReceipt(reservation_id, self._revision, disposition)
            for scope in fact.scopes:
                remaining = self._counts[scope] - 1
                if remaining:
                    self._counts[scope] = remaining
                else:
                    del self._counts[scope]
            self._settled.add(reservation_id)
            self._revision += 1
            self._facts.append(LogicalCapacityFact(self._revision, reservation_id, fact.scopes, False))
            self._persist_or_restore(prior)
            return LogicalCapacitySettlementReceipt(
                reservation_id, self._revision, CapacitySettlementDisposition.SETTLED
            )

    def count(self, scope: LogicalCapacityScope) -> int:
        with self._lock:
            return self._counts.get(scope, 0)

    def facts(self) -> tuple[LogicalCapacityFact, ...]:
        with self._lock:
            return tuple(self._facts)

    def reservation(self, reservation_id: str) -> LogicalCapacityFact | None:
        with self._lock:
            return self._active.get(reservation_id)

    @classmethod
    def rebuild(cls, facts: tuple[LogicalCapacityFact, ...]) -> "LogicalCapacityProjection":
        projection = cls()
        for fact in facts:
            if fact.revision != projection._revision + 1:
                raise ValueError("logical capacity fact revision gap")
            if fact.active:
                if fact.reservation_id in projection._active or fact.reservation_id in projection._settled:
                    raise ValueError("logical capacity reservation identity was reused")
                projection._active[fact.reservation_id] = fact
                for scope in fact.scopes:
                    projection._counts[scope] = projection._counts.get(scope, 0) + 1
            else:
                active = projection._active.pop(fact.reservation_id, None)
                if active is None or active.scopes != fact.scopes:
                    raise ValueError("logical capacity settlement has no matching reservation")
                for scope in fact.scopes:
                    remaining = projection._counts[scope] - 1
                    if remaining:
                        projection._counts[scope] = remaining
                    else:
                        del projection._counts[scope]
                projection._settled.add(fact.reservation_id)
            projection._facts.append(fact)
            projection._revision = fact.revision
        return projection

    def _persist_or_restore(self, prior: tuple[LogicalCapacityFact, ...]) -> None:
        if self._path is None:
            return
        try:
            payload = {
                "schema": "mote.agent-logical-capacity/v1",
                "facts": [
                    {
                        "revision": fact.revision,
                        "reservation_id": fact.reservation_id,
                        "scopes": [{"kind": scope.kind.value, "identity": scope.identity} for scope in fact.scopes],
                        "active": fact.active,
                    }
                    for fact in self._facts
                ],
            }
            atomic_write(
                self._path,
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        except BaseException:
            self._adopt(self.rebuild(prior))
            raise

    def _adopt(self, projection: "LogicalCapacityProjection") -> None:
        self._revision = projection._revision
        self._counts = dict(projection._counts)
        self._active = dict(projection._active)
        self._settled = set(projection._settled)
        self._facts = list(projection._facts)

    @classmethod
    def _decode(cls, path: Path) -> "LogicalCapacityProjection":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        if type(raw) is not dict or set(raw) != {"schema", "facts"}:
            raise ValueError("logical capacity envelope shape is invalid")
        if raw["schema"] != "mote.agent-logical-capacity/v1" or type(raw["facts"]) is not list:
            raise ValueError("logical capacity envelope is invalid")
        facts = []
        for item in raw["facts"]:
            if type(item) is not dict or set(item) != {"revision", "reservation_id", "scopes", "active"}:
                raise ValueError("logical capacity fact shape is invalid")
            if (
                type(item["revision"]) is not int
                or type(item["reservation_id"]) is not str
                or not item["reservation_id"]
            ):
                raise ValueError("logical capacity fact identity is invalid")
            if type(item["active"]) is not bool or type(item["scopes"]) is not list:
                raise ValueError("logical capacity fact primitive is invalid")
            scopes = []
            for scope in item["scopes"]:
                if type(scope) is not dict or set(scope) != {"kind", "identity"}:
                    raise ValueError("logical capacity scope shape is invalid")
                if type(scope["kind"]) is not str or type(scope["identity"]) is not str:
                    raise ValueError("logical capacity scope primitive is invalid")
                scopes.append(LogicalCapacityScope(LogicalCapacityScopeKind(scope["kind"]), scope["identity"]))
            facts.append(LogicalCapacityFact(item["revision"], item["reservation_id"], tuple(scopes), item["active"]))
        return cls.rebuild(tuple(facts))


__all__ = ["LogicalCapacityFact", "LogicalCapacityProjection"]
