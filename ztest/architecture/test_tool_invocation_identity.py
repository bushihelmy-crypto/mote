from __future__ import annotations

from pathlib import Path

import pytest

from mote.contracts.tool import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity, tool_arguments_digest

ROOT = Path(__file__).resolve().parents[2]


def test_invocation_identity_is_nominal_strict_and_argument_order_independent():
    assert tool_arguments_digest({"b": 2, "a": 1}) == tool_arguments_digest({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        ToolInvocationId("")
    with pytest.raises(ValueError):
        ToolAttemptOrdinal(0)
    with pytest.raises((TypeError, ValueError)):
        tool_arguments_digest({"bad": float("nan")})

    identity = ToolInvocationIdentity(
        ToolInvocationId("logical-call"),
        ToolAttemptOrdinal(2),
        "definition",
        3,
        tool_arguments_digest({"x": 1}),
        "agent-incarnation",
        "run",
    )
    assert identity.with_arguments({"x": 2}).invocation_id == identity.invocation_id
    assert identity.with_arguments({"x": 2}).attempt_ordinal == identity.attempt_ordinal
    assert identity.with_arguments({"x": 2}).arguments_digest != identity.arguments_digest
    assert ToolInvocationIdentity.from_payload(identity.to_payload()) == identity
    with pytest.raises(ValueError):
        ToolInvocationIdentity.from_payload({**identity.to_payload(), "unknown": True})


def test_transports_only_project_execution_owner_identity():
    acp = (ROOT / "product/interfaces/acp/wire.py").read_text(encoding="utf-8")
    agui = (ROOT / "product/interfaces/agui/wire.py").read_text(encoding="utf-8")

    assert "str(e.identity.invocation_id)" in acp
    assert "str(e.identity.invocation_id)" in agui
    assert "id(e)" not in agui
    assert "id(event)" not in agui
    assert "e.tool_use_id or" not in agui
    assert "_block_seq" not in acp.split("def _tool_id", 1)[1].split("def _promote_to_call", 1)[0]


def test_tool_fact_and_view_contracts_cannot_omit_invocation_identity():
    facts = (ROOT / "contracts/events/tool.py").read_text(encoding="utf-8")
    views = (ROOT / "product/presentation/events/events.py").read_text(encoding="utf-8")
    settlement = (ROOT / "runtime/tools/tool_settlement.py").read_text(encoding="utf-8")

    assert "tool_use_id: Optional" not in facts
    assert "tool_use_id: Optional" not in views
    assert facts.count("identity: ToolInvocationIdentity") == 2
    assert views.count("identity: ToolInvocationIdentity") == 5
    assert "identity=identity" in settlement


def test_tool_effect_journal_persists_the_full_identity():
    pipeline = (ROOT / "runtime/tools/tool_pipeline.py").read_text(encoding="utf-8")
    journal = (ROOT / "runtime/ledger/run_journal.py").read_text(encoding="utf-8")

    assert "invocation_identity=execution.identity" in pipeline
    assert "invocation_identity=prior.invocation_identity" in journal
    assert "ToolInvocationIdentity.from_payload" in journal
    assert "execution.identity = durable_identity" in pipeline


def test_executor_is_the_only_missing_id_mint_and_pipeline_carries_full_identity():
    executor = (ROOT / "runtime/tools/tool_executor.py").read_text(encoding="utf-8")
    pipeline = (ROOT / "runtime/tools/tool_pipeline.py").read_text(encoding="utf-8")
    policy = (ROOT / "runtime/tools/policy.py").read_text(encoding="utf-8")

    assert 'result_id or f"tool-{uuid.uuid4().hex}"' in executor
    assert "identity: ToolInvocationIdentity" in pipeline
    assert "result_id: str | None" not in pipeline
    assert "intent.identity.with_arguments(arguments)" in policy


def test_executor_does_not_publish_live_catalog_or_tool_instances():
    executor = (ROOT / "runtime/tools/tool_executor.py").read_text(encoding="utf-8")
    views = (ROOT / "runtime/tools/tool_views.py").read_text(encoding="utf-8")
    catalog = (ROOT / "runtime/tools/tool_catalog.py").read_text(encoding="utf-8")

    assert "def catalog(" not in executor
    assert "def catalog(" not in views
    assert "def tools(" not in catalog
