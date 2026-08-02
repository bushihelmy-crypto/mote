from datetime import datetime
from typing import Protocol

from mote.contracts.inference.governance import (
    BudgetDimension,
    BudgetReservation,
    BudgetReservationRequest,
    BudgetScope,
    UsageSettlement,
)


class UsageLedger(Protocol):
    def reservations_by_id(self, reservation_ids: tuple[str, ...]) -> tuple[BudgetReservation, ...]: ...

    async def reserve_many(
        self,
        requests: tuple[BudgetReservationRequest, ...],
        *,
        ttl_seconds: float,
    ) -> tuple[BudgetReservation, ...]: ...

    async def reserve(
        self,
        *,
        reservation_id: str,
        attempt_id: str,
        tenant_id: str,
        project_id: str,
        units: int,
        ttl_seconds: float,
        dimension: BudgetDimension = BudgetDimension.INFERENCE_UNIT,
        scopes: tuple[BudgetScope, ...] = (),
    ) -> BudgetReservation: ...

    async def settle(
        self,
        reservation: BudgetReservation,
        *,
        settlement_id: str,
        actual_units: int,
    ) -> UsageSettlement: ...

    async def release(self, reservation: BudgetReservation, *, settlement_id: str) -> UsageSettlement: ...

    async def pending_reconciliation(
        self, reservation: BudgetReservation, *, settlement_id: str
    ) -> UsageSettlement: ...

    async def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        settlement_id: str,
        actual_units: int,
        fencing_token: int,
    ) -> UsageSettlement: ...

    async def reclaim_expired(self, *, now: datetime, fencing_token: int) -> tuple[UsageSettlement, ...]: ...
