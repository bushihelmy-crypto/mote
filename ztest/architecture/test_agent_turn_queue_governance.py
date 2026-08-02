from __future__ import annotations

import ast
from pathlib import Path

from mote.orchestration.agents.turn_queue.codec import TURN_QUEUE_SCHEMA
from mote.orchestration.agents.turn_queue.scheduling import MAX_ROOT_WEIGHT
from mote.product.composition.turn_scheduling import compile_turn_scheduling
from mote.product.config.agents import AgentGovernanceConfig

ROOT = Path(__file__).resolve().parents[2]
TURN_QUEUE = ROOT / "orchestration/agents/turn_queue"


def test_turn_queue_is_agent_owned_and_does_not_duplicate_adjacent_state_machines() -> None:
    forbidden = (
        "mote.runtime.inference.fair_queue",
        "mote.orchestration.workflows",
        "mote.orchestration.automation",
        "mote.orchestration.agents.messaging.pending",
        "mote.orchestration.agents.messaging.mailbox",
    )
    imports: set[str] = set()
    for path in TURN_QUEUE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
    assert all(not any(module.startswith(prefix) for prefix in forbidden) for module in imports)
    assert TURN_QUEUE_SCHEMA == "mote.agent-turn-queue/v1"


def test_turn_queue_reuses_canonical_fence_clock_atomic_write_and_execution_permit() -> None:
    store = (TURN_QUEUE / "store.py").read_text(encoding="utf-8")
    scheduler = (TURN_QUEUE / "scheduler.py").read_text(encoding="utf-8")
    model = (TURN_QUEUE / "model.py").read_text(encoding="utf-8")
    assert "LeaseCoordinator" in store and "assert_current" in store
    assert "disk_io.atomic_write" in store and "fsync=True" in store
    assert "AgentExecutionLimiter" in scheduler and "guard.receipt" in scheduler
    assert "AbsoluteInstant" in model
    assert "delivery_id" in model and "Message" not in model
    assert 1 < MAX_ROOT_WEIGHT <= 64


def test_product_schema_bounds_root_weight_and_compiles_explicit_generation() -> None:
    product = AgentGovernanceConfig(root_weights={"root-b": 2, "root-a": 3})
    compiled = compile_turn_scheduling(product, generation=7)
    assert compiled.generation == 7
    assert tuple((weight.root_id, weight.units) for weight in compiled.root_weights) == (
        ("root-a", 3),
        ("root-b", 2),
    )
