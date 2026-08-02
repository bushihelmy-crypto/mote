from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from mote.contracts.model.turn import ModelTurn, TextAction
from mote.contracts.output.completion import CompletionDecision, CompletionKind
from mote.contracts.ports.execution.model_turn_completion import ModelTurnCompletionPolicy
from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicy
from mote.kernel.execution.operations.completion import TextCompletionPolicy

ROOT = Path(__file__).resolve().parents[2]


def test_model_turn_completion_port_is_narrow_and_lifecycle_specific() -> None:
    assert set(ModelTurnCompletionPolicy.__dict__) & {"evaluate"} == {"evaluate"}
    assert "process" not in ModelTurnCompletionPolicy.__dict__
    assert "process" in RunCompletionPolicy.__dict__
    assert "evaluate" not in RunCompletionPolicy.__dict__


def test_kernel_engine_has_no_inline_completion_protocol() -> None:
    source = (ROOT / "kernel/execution/engine.py").read_text(encoding="utf-8")

    assert "class CompletionPolicy" not in source
    assert "ModelTurnCompletionPolicy" in source


def test_graph_assembly_and_interpret_nodes_use_the_contract_port() -> None:
    for relative in (
        "kernel/execution/operations/container.py",
        "kernel/execution/graph/react.py",
        "kernel/execution/graph/review_refine.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "ModelTurnCompletionPolicy" in source
        assert "completion_policy: Any" not in source


def test_text_policy_satisfies_model_turn_contract() -> None:
    signature = inspect.signature(TextCompletionPolicy.evaluate)
    assert signature.parameters["turn"].annotation is ModelTurn

    decision = asyncio.run(
        TextCompletionPolicy().evaluate(ModelTurn(content="continue", actions=[TextAction(content="continue")]))
    )
    assert decision.kind.value == "continue"


@pytest.mark.parametrize(
    ("kind", "candidate_index", "reason"),
    [
        (CompletionKind.VALIDATE_CANDIDATE, None, ""),
        (CompletionKind.VALIDATE_CANDIDATE, -1, ""),
        (CompletionKind.CONTINUE, 0, ""),
        (CompletionKind.CONTINUE, None, "unexpected"),
        (CompletionKind.FAIL, None, ""),
    ],
)
def test_completion_decision_rejects_contradictory_states(kind, candidate_index, reason) -> None:
    with pytest.raises(ValueError):
        CompletionDecision(kind, candidate_index=candidate_index, reason=reason)
