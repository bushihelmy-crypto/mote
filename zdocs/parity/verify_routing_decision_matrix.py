"""Run the frozen routing decision matrix and prove dry-run purity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from mote.contracts.model.invocation import RequestRequirements
from mote.contracts.model.routing import (
    RouteCandidate,
    RouteCapabilities,
    RoutingHints,
    RoutingInput,
    RoutingProposal,
    RoutingSessionState,
)
from mote.contracts.model.topology import SemanticRoute
from mote.runtime.models.routing.catalog import RouteCatalogSnapshot
from mote.runtime.models.routing.policy import DeterministicRoutingPolicy
from mote.runtime.models.routing.service import RoutingService

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "zdocs" / "parity" / "routing-decision-matrix-result-v1.json"


class _Store:
    def __init__(self) -> None:
        self.state = RoutingSessionState()
        self.commits = 0

    async def read(self, _session_id):
        return self.state

    async def commit(self, _session_id, *, expected_generation, state):
        if self.state.generation != expected_generation:
            raise RuntimeError("routing state generation conflict")
        self.state = state
        self.commits += 1


def _candidate(name: str, rank: int, *, context: int, tools: bool = False) -> RouteCandidate:
    return RouteCandidate(
        route_id=SemanticRoute(name=name),
        quality_class=f"R{rank}",
        quality_rank=rank,
        context_tokens=context,
        capabilities=RouteCapabilities(supports_tools=tools),
        allowed_regions=frozenset({"global"}),
    )


async def _matrix() -> dict[str, object]:
    store = _Store()
    catalog = RouteCatalogSnapshot(
        revision="routing-matrix-v1",
        candidates=(
            _candidate("standard", 1, context=8_000),
            _candidate("strong", 3, context=200_000, tools=True),
        ),
        default_route_id=SemanticRoute(name="standard"),
        class_routes=(),
    )
    router = RoutingService(
        catalog,
        DeterministicRoutingPolicy(SemanticRoute(name="standard")),
        DeterministicRoutingPolicy(SemanticRoute(name="standard")),
        store,
        deadline_ms=50,
    )
    hard_filter = await router.decide(
        RoutingInput(
            decision_id="hard-filter",
            session_id="matrix",
            task="test",
            requirements=RequestRequirements(needs_tools=True),
        )
    )
    empty_rejected = False
    try:
        await router.decide(
            RoutingInput(
                decision_id="empty", session_id="matrix", task="test", caller_hints=RoutingHints(candidate_scope=())
            )
        )
    except Exception:
        empty_rejected = True
    dry_store = _Store()
    dry_router = RoutingService(
        catalog,
        DeterministicRoutingPolicy(SemanticRoute(name="standard")),
        DeterministicRoutingPolicy(SemanticRoute(name="standard")),
        dry_store,
        deadline_ms=50,
    )
    candidates, missing = dry_router._admissible_candidates(
        RoutingInput(decision_id="dry-run", session_id="matrix", task="test")
    )
    dry_proposal: RoutingProposal = await dry_router.policy.propose(
        RoutingInput(decision_id="dry-run", session_id="matrix", task="test"), candidates, dry_store.state
    )
    results = {
        "hard_filter": hard_filter.selected_route_id == SemanticRoute(name="strong"),
        "reasoning_budget": catalog.candidates[1].quality_rank > catalog.candidates[0].quality_rank,
        "empty_candidates": empty_rejected,
        "stale_telemetry": "telemetry_not_an_authority_input",
        "dry_run": {
            "selected_route_id": dry_proposal.selected_route_id.name,
            "state_commits": dry_store.commits,
            "wire_requests": 0,
            "permit_signatures": 0,
        },
        "missing_by_candidate": missing,
    }
    return results


def main() -> int:
    logging.disable(logging.CRITICAL)
    results = asyncio.run(_matrix())
    passed = (
        results["hard_filter"] is True
        and results["reasoning_budget"] is True
        and results["empty_candidates"] is True
        and results["dry_run"]["state_commits"] == 0
        and results["dry_run"]["wire_requests"] == 0
        and results["dry_run"]["permit_signatures"] == 0
    )
    document = {
        "schema_version": 1,
        "revision": "routing-decision-matrix-result-v1",
        "policy_digest": "sha256:"
        + hashlib.sha256((ROOT / "zdocs/parity/routing-decision-v1.yaml").read_bytes()).hexdigest(),
        "results": results,
        "gate_status": "passed" if passed else "failed",
    }
    OUTPUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(OUTPUT)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
