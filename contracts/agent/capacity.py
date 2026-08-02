"""Typed identities and receipts for the three independent Agent capacities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LogicalCapacityScopeKind(StrEnum):
    APPLICATION = "application"
    ROOT = "root"
    SUBTREE = "subtree"
    PARENT = "parent"


@dataclass(frozen=True, slots=True)
class LogicalCapacityScope:
    kind: LogicalCapacityScopeKind
    identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LogicalCapacityScopeKind):
            raise TypeError("logical capacity scope kind is invalid")
        if type(self.identity) is not str or not self.identity:
            raise ValueError("logical capacity scope identity is invalid")


@dataclass(frozen=True, slots=True)
class LogicalCapacityLimit:
    scope: LogicalCapacityScope
    maximum: int

    def __post_init__(self) -> None:
        if type(self.maximum) is not int or self.maximum < 1:
            raise ValueError("logical capacity maximum must be positive")


class CapacityReservationDisposition(StrEnum):
    RESERVED = "reserved"
    REJECTED_CAPACITY = "rejected_capacity"
    REVISION_CONFLICT = "revision_conflict"


class CapacitySettlementDisposition(StrEnum):
    SETTLED = "settled"
    ALREADY_SETTLED = "already_settled"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"


@dataclass(frozen=True, slots=True)
class LogicalCapacityReservationReceipt:
    reservation_id: str
    revision: int
    scopes: tuple[LogicalCapacityScope, ...]
    disposition: CapacityReservationDisposition


@dataclass(frozen=True, slots=True)
class LogicalCapacitySettlementReceipt:
    reservation_id: str
    revision: int
    disposition: CapacitySettlementDisposition


@dataclass(frozen=True, slots=True)
class ResidentCapacityReservationReceipt:
    reservation_id: str
    incarnation_generation: int


@dataclass(frozen=True, slots=True)
class ResidentCapacitySettlementReceipt:
    reservation_id: str
    incarnation_generation: int
    disposition: CapacitySettlementDisposition


@dataclass(frozen=True, slots=True)
class TurnCapacityPermitReceipt:
    permit_id: str


@dataclass(frozen=True, slots=True)
class TurnCapacitySettlementReceipt:
    permit_id: str
    disposition: CapacitySettlementDisposition


__all__ = [
    "CapacityReservationDisposition",
    "CapacitySettlementDisposition",
    "LogicalCapacityLimit",
    "LogicalCapacityReservationReceipt",
    "LogicalCapacityScope",
    "LogicalCapacityScopeKind",
    "LogicalCapacitySettlementReceipt",
    "ResidentCapacityReservationReceipt",
    "ResidentCapacitySettlementReceipt",
    "TurnCapacityPermitReceipt",
    "TurnCapacitySettlementReceipt",
]
