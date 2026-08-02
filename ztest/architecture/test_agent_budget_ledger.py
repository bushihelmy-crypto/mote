from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3

import pytest

from mote.contracts.agent.budget import AgentBudgetDisposition, AgentBudgetPolicy, AgentBudgetRequest
from mote.contracts.inference.governance import BudgetDimension
from mote.orchestration.agents.budget import AgentBudgetCoordinator
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore, SQLiteIntegrityError, SQLiteUsageLedger
from mote.runtime.clock import SystemClock


def _request(request_id: str, *, subtree: str = "s", tokens: int | None = 60) -> AgentBudgetRequest:
    return AgentBudgetRequest(
        request_id=request_id,
        root_id="root",
        subtree_id=subtree,
        agent_id=f"agent-{request_id}",
        requested_tokens=tokens,
        requested_cost_micro_usd=20,
        child_depth=2,
        capabilities=frozenset({"terminal"}),
    )


def _policy() -> AgentBudgetPolicy:
    return AgentBudgetPolicy(100, 100, 3, frozenset({"terminal", "browser"}))


async def _configure(
    ledger: SQLiteUsageLedger,
    coordinator: AgentBudgetCoordinator,
    requests: tuple[AgentBudgetRequest, ...],
) -> None:
    configured: set[tuple[str, str]] = set()
    allocations = (
        (BudgetDimension.TOKEN, "token", 100),
        (BudgetDimension.COST_MICRO_USD, "cost", 100),
        (BudgetDimension.DEPTH, "depth:2", 100),
        (BudgetDimension.CAPABILITY, "capability:terminal", 100),
    )
    for request in requests:
        for dimension, label, limit in allocations:
            for scope in coordinator.scopes_for(request, dimension, label):
                key = (scope.tenant_id, scope.project_id)
                if key not in configured:
                    await ledger.configure_budget(*key, limit)
                    configured.add(key)


def test_root_and_subtree_reservation_is_atomic_and_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        authority = SQLiteAttemptReceiptStore(tmp_path / "usage.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority, clock_source=SystemClock())
        coordinator = AgentBudgetCoordinator(ledger)
        first_request = _request("one", subtree="a")
        second_request = _request("two", subtree="b")
        await _configure(ledger, coordinator, (first_request, second_request))
        first, second = await asyncio.gather(
            coordinator.reserve(first_request, _policy()),
            coordinator.reserve(second_request, _policy()),
        )
        assert {first.disposition, second.disposition} == {
            AgentBudgetDisposition.RESERVED,
            AgentBudgetDisposition.REJECTED_BUDGET,
        }
        accepted_request = first_request if first.disposition is AgentBudgetDisposition.RESERVED else second_request
        rejected_request = second_request if accepted_request is first_request else first_request
        accepted = first if first.disposition is AgentBudgetDisposition.RESERVED else second
        assert await coordinator.reserve(accepted_request, _policy()) == accepted
        await coordinator.settle(accepted, actual_tokens=30, actual_cost_micro_usd=10)
        retried = await coordinator.reserve(rejected_request, _policy())
        assert retried.disposition is AgentBudgetDisposition.RESERVED

    asyncio.run(scenario())


def test_actual_settlement_refunds_unused_units_across_all_scopes(tmp_path) -> None:
    async def scenario() -> None:
        authority = SQLiteAttemptReceiptStore(tmp_path / "usage.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority, clock_source=SystemClock())
        coordinator = AgentBudgetCoordinator(ledger)
        first_request = _request("one")
        second_request = _request("two")
        await _configure(ledger, coordinator, (first_request, second_request))
        first = await coordinator.reserve(first_request, _policy())
        assert first.disposition is AgentBudgetDisposition.RESERVED
        recovered = coordinator.reservations_by_id(tuple(item.reservation_id for item in first.reservations))
        assert recovered == first.reservations
        settlement = await coordinator.settle(first, actual_tokens=40, actual_cost_micro_usd=10)
        assert {item.actual_units for item in settlement.settlements} >= {0, 10, 40}
        second = await coordinator.reserve(second_request, _policy())
        assert second.disposition is AgentBudgetDisposition.RESERVED

    asyncio.run(scenario())


def test_partial_agent_settlement_recovers_from_canonical_reservations(tmp_path) -> None:
    async def scenario() -> None:
        authority = SQLiteAttemptReceiptStore(tmp_path / "usage.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority, clock_source=SystemClock())
        coordinator = AgentBudgetCoordinator(ledger)
        request = _request("partial")
        await _configure(ledger, coordinator, (request,))
        receipt = await coordinator.reserve(request, _policy())
        first = receipt.reservations[0]
        await ledger.settle(
            first,
            settlement_id=f"agent-budget-settle:{first.reservation_id}",
            actual_units=40,
        )
        recovered = coordinator.reservations_by_id(tuple(item.reservation_id for item in receipt.reservations))
        recovered_receipt = receipt.__class__(receipt.request_id, receipt.disposition, recovered)
        settlement = await coordinator.settle(
            recovered_receipt,
            actual_tokens=40,
            actual_cost_micro_usd=10,
        )
        assert len(settlement.settlements) == len(receipt.reservations)

    asyncio.run(scenario())


def test_unknown_usage_and_extension_expansion_fail_closed(tmp_path) -> None:
    async def scenario() -> None:
        authority = SQLiteAttemptReceiptStore(tmp_path / "usage.sqlite3")
        await authority.initialize()
        coordinator = AgentBudgetCoordinator(SQLiteUsageLedger(authority, clock_source=SystemClock()))
        denied = await coordinator.reserve(_request("unknown", tokens=None), _policy())
        assert denied.disposition is AgentBudgetDisposition.REJECTED_POLICY
        assert "unknown" in denied.reason

    asyncio.run(scenario())
    narrowed = AgentBudgetPolicy(50, 50, 2, frozenset({"terminal"}))
    assert _policy().narrowed_by(narrowed) is narrowed
    try:
        narrowed.narrowed_by(_policy())
    except ValueError as error:
        assert "expand" in str(error)
    else:
        raise AssertionError("budget expansion must fail closed")


def test_v1_inference_projection_fails_closed(tmp_path) -> None:
    database = tmp_path / "usage.sqlite3"

    async def seed() -> None:
        authority = SQLiteAttemptReceiptStore(database)
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority, clock_source=SystemClock())
        await ledger.configure_budget("tenant", "project", 10)
        await ledger.reserve(
            reservation_id="legacy",
            attempt_id="legacy-attempt",
            tenant_id="tenant",
            project_id="project",
            units=2,
            ttl_seconds=30,
        )

    asyncio.run(seed())
    with sqlite3.connect(database) as connection:
        raw = json.loads(
            connection.execute("SELECT payload FROM usage_reservations WHERE reservation_id = 'legacy'").fetchone()[0]
        )
        raw["schema_version"] = 1
        raw.pop("dimension")
        raw.pop("scopes")
        connection.execute(
            "UPDATE usage_reservations SET payload = ? WHERE reservation_id = 'legacy'",
            (json.dumps(raw),),
        )
        connection.execute("DELETE FROM usage_reservation_scopes WHERE reservation_id = 'legacy'")

    async def verify() -> None:
        authority = SQLiteAttemptReceiptStore(database)
        with pytest.raises(SQLiteIntegrityError, match="schema is unknown"):
            await authority.initialize()

    asyncio.run(verify())


def test_usage_ledger_requires_clock_port_and_rejects_boolean_commands(tmp_path) -> None:
    async def scenario() -> None:
        authority = SQLiteAttemptReceiptStore(tmp_path / "strict.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority, clock_source=SystemClock())
        with pytest.raises(ValueError):
            await ledger.configure_budget("tenant", "project", True)
        await ledger.configure_budget("tenant", "project", 10)
        with pytest.raises(ValueError):
            await ledger.reserve(
                reservation_id="bad",
                attempt_id="bad-attempt",
                tenant_id="tenant",
                project_id="project",
                units=True,
                ttl_seconds=1,
            )

    asyncio.run(scenario())

    source = inspect.getsource(SQLiteUsageLedger)
    assert "clock_source: ClockSource" in source
    assert "datetime.now" not in source
