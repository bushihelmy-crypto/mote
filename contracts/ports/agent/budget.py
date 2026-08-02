"""Consumer-owned Agent budget reservation and settlement Port."""

from typing import Protocol

from mote.contracts.agent.budget import (
    AgentBudgetPolicy,
    AgentBudgetRequest,
    AgentBudgetReservationReceipt,
    AgentBudgetSettlementReceipt,
)
from mote.contracts.inference.governance import BudgetReservation


class AgentBudgetPort(Protocol):
    def reservations_by_id(self, reservation_ids: tuple[str, ...]) -> tuple[BudgetReservation, ...]: ...

    async def reserve(
        self, request: AgentBudgetRequest, policy: AgentBudgetPolicy
    ) -> AgentBudgetReservationReceipt: ...

    async def settle(
        self,
        receipt: AgentBudgetReservationReceipt,
        *,
        actual_tokens: int,
        actual_cost_micro_usd: int,
    ) -> AgentBudgetSettlementReceipt: ...


__all__ = ["AgentBudgetPort"]
