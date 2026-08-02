"""Agent governance coordination over the canonical UsageLedger."""

from __future__ import annotations

import hashlib

from mote.contracts.agent.budget import (
    AgentBudgetDisposition,
    AgentBudgetPolicy,
    AgentBudgetRequest,
    AgentBudgetReservationReceipt,
    AgentBudgetSettlementReceipt,
)
from mote.contracts.inference.governance import (
    BudgetAdmissionError,
    BudgetDimension,
    BudgetReservation,
    BudgetReservationRequest,
    BudgetScope,
    BudgetScopeKind,
)
from mote.contracts.ports.inference.budget_configuration import BudgetLimitConfigurator
from mote.contracts.ports.inference.usage_ledger import UsageLedger


class AgentBudgetCoordinator:
    """Reserve and settle Agent dimensions without owning a second ledger."""

    def __init__(
        self,
        ledger: UsageLedger,
        *,
        configurator: BudgetLimitConfigurator | None = None,
        ttl_seconds: float = 300.0,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Agent budget reservation ttl must be positive")
        self._ledger = ledger
        self._configurator = configurator
        self._ttl_seconds = ttl_seconds

    def reservations_by_id(self, reservation_ids: tuple[str, ...]) -> tuple[BudgetReservation, ...]:
        return self._ledger.reservations_by_id(reservation_ids)

    async def reserve(
        self,
        request: AgentBudgetRequest,
        policy: AgentBudgetPolicy,
    ) -> AgentBudgetReservationReceipt:
        denial = self._policy_denial(request, policy)
        if denial:
            return AgentBudgetReservationReceipt(
                request.request_id,
                AgentBudgetDisposition.REJECTED_POLICY,
                reason=denial,
            )
        assert request.requested_tokens is not None
        assert request.requested_cost_micro_usd is not None
        allocations = [
            (BudgetDimension.TOKEN, request.requested_tokens, "token"),
            (BudgetDimension.COST_MICRO_USD, request.requested_cost_micro_usd, "cost"),
            (BudgetDimension.DEPTH, 1, f"depth:{request.child_depth}"),
        ]
        allocations.extend(
            (BudgetDimension.CAPABILITY, 1, f"capability:{capability}") for capability in sorted(request.capabilities)
        )
        reservation_requests: list[BudgetReservationRequest] = []
        for dimension, units, label in allocations:
            if units == 0:
                continue
            scopes = self.scopes_for(request, dimension, label)
            identity = self._allocation_identity(request.request_id, label)
            reservation_requests.append(
                BudgetReservationRequest(
                    reservation_id=identity,
                    attempt_id=identity,
                    tenant_id=scopes[0].tenant_id,
                    project_id=scopes[0].project_id,
                    units=units,
                    dimension=dimension,
                    scopes=scopes,
                )
            )
        if self._configurator is not None:
            limits = {
                BudgetDimension.TOKEN: policy.max_tokens,
                BudgetDimension.COST_MICRO_USD: policy.max_cost_micro_usd,
                BudgetDimension.DEPTH: policy.max_depth,
                BudgetDimension.CAPABILITY: policy.max_depth,
            }
            configured: set[tuple[str, str]] = set()
            for reservation_request in reservation_requests:
                for scope in reservation_request.scopes:
                    key = (scope.tenant_id, scope.project_id)
                    if key in configured:
                        continue
                    await self._configurator.configure_budget(*key, limits[reservation_request.dimension])
                    configured.add(key)
        try:
            reservations = await self._ledger.reserve_many(
                tuple(reservation_requests),
                ttl_seconds=self._ttl_seconds,
            )
        except BudgetAdmissionError as exc:
            return AgentBudgetReservationReceipt(
                request.request_id,
                AgentBudgetDisposition.REJECTED_BUDGET,
                reason=exc.disposition.value,
            )
        except Exception:
            return AgentBudgetReservationReceipt(
                request.request_id,
                AgentBudgetDisposition.IN_DOUBT,
                reason="usage ledger unavailable",
            )
        return AgentBudgetReservationReceipt(
            request.request_id,
            AgentBudgetDisposition.RESERVED,
            reservations,
        )

    async def settle(
        self,
        receipt: AgentBudgetReservationReceipt,
        *,
        actual_tokens: int,
        actual_cost_micro_usd: int,
    ) -> AgentBudgetSettlementReceipt:
        if receipt.disposition is not AgentBudgetDisposition.RESERVED:
            raise ValueError("only a reserved Agent budget receipt can settle")
        if actual_tokens < 0 or actual_cost_micro_usd < 0:
            raise ValueError("actual Agent usage cannot be negative")
        settlements = []
        for reservation in receipt.reservations:
            settlement_id = f"agent-budget-settle:{reservation.reservation_id}"
            if reservation.dimension is BudgetDimension.TOKEN:
                actual = actual_tokens
            elif reservation.dimension is BudgetDimension.COST_MICRO_USD:
                actual = actual_cost_micro_usd
            else:
                settlements.append(await self._ledger.release(reservation, settlement_id=settlement_id))
                continue
            settlements.append(
                await self._ledger.settle(
                    reservation,
                    settlement_id=settlement_id,
                    actual_units=actual,
                )
            )
        return AgentBudgetSettlementReceipt(receipt.request_id, tuple(settlements))

    @staticmethod
    def scopes_for(
        request: AgentBudgetRequest,
        dimension: BudgetDimension,
        label: str,
    ) -> tuple[BudgetScope, BudgetScope]:
        suffix = hashlib.sha256(label.encode("utf-8")).hexdigest()
        tenant = f"agent-root:{request.root_id}"
        dimension_key = dimension.value
        return (
            BudgetScope(
                kind=BudgetScopeKind.AGENT_ROOT,
                tenant_id=tenant,
                project_id=f"{dimension_key}:root:{suffix}",
            ),
            BudgetScope(
                kind=BudgetScopeKind.AGENT_SUBTREE,
                tenant_id=tenant,
                project_id=f"{dimension_key}:subtree:{request.subtree_id}:{suffix}",
            ),
        )

    @staticmethod
    def _allocation_identity(request_id: str, label: str) -> str:
        digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
        return f"agent-budget:{request_id}:{digest}"

    @staticmethod
    def _policy_denial(request: AgentBudgetRequest, policy: AgentBudgetPolicy) -> str:
        if request.requested_tokens is None or request.requested_cost_micro_usd is None:
            return "unknown usage cannot be admitted"
        if request.requested_tokens > policy.max_tokens:
            return "token request exceeds delegated policy"
        if request.requested_cost_micro_usd > policy.max_cost_micro_usd:
            return "cost request exceeds delegated policy"
        if request.child_depth > policy.max_depth:
            return "child depth exceeds delegated policy"
        if not request.capabilities.issubset(policy.capabilities):
            return "capability request exceeds delegated policy"
        return ""


__all__ = ["AgentBudgetCoordinator"]
