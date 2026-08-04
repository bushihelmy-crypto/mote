"""Model execution crosses one finalized, identity-bound generate contract."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mote.contracts.model.inference import (
    FinalizedGenerateRequest,
    InferenceAttemptFence,
    InferenceIntent,
    InferenceRequirements,
    InferenceResult,
)
from mote.contracts.model.invocation import GenerateOutput, ResolvedModelResponse

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_model_client_and_wide_generate_entry_are_retired() -> None:
    assert not (ROOT / "contracts/ports/model/client.py").exists()
    source = (ROOT / "runtime/models/model_calls.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    public_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }
    assert "generate" not in public_functions
    assert "generate_finalized" in public_functions
    assert "canonical_messages_from" not in public_functions


def test_internal_generate_consumers_use_finalized_request() -> None:
    for relative in (
        "runtime/agent/components/session.py",
        "runtime/context/compaction/reducers/summarize.py",
        "runtime/models/inference_port.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "FinalizedGenerateRequest" in source or "FinalizedInferenceRequest" in source
        assert "generate_finalized" in source
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "mote.runtime.models.model_calls"
            and any(alias.name == "generate" for alias in node.names)
            for node in ast.walk(tree)
        )


def test_inference_contract_is_frozen_strict_and_json_typed() -> None:
    result = InferenceResult(structured_value={"answer": [1, True]})
    with pytest.raises(Exception):
        result.content = "changed"
    with pytest.raises(Exception):
        InferenceResult(structured_value={"unsafe": object()})

    # Empty canonical message sets fail closed before provider execution.
    with pytest.raises(TypeError):
        FinalizedGenerateRequest(messages=(), task="interactive")


def test_model_call_and_attempt_identity_fail_closed() -> None:
    with pytest.raises(ValueError):
        InferenceIntent("", requirements=InferenceRequirements())
    with pytest.raises(ValueError):
        InferenceAttemptFence("call", "attempt", 0)
    with pytest.raises(Exception):
        ResolvedModelResponse(
            output=GenerateOutput(content="done"),
            endpoint_id="endpoint",
            endpoint_fingerprint="fingerprint",
            model_or_deployment="model",
            tenant_fingerprint="tenant",
            credential_slot_id="slot",
        )
