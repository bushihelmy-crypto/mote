from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.execution.models import InferenceCheckpointState
from mote.contracts.model import (
    AttemptBudget,
    ModelCallFinishedRecord,
    ModelCallPlannedRecord,
    ModelCallRecovery,
    ModelCallState,
)
from mote.contracts.model.invocation import CanonicalModelResponse, GenerateOutput
from mote.contracts.ports.model.recovery import ModelRecoveryDisposition, ModelRecoveryInspection
from mote.contracts.tool.identity import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.product.migrations.run_domains import activate_candidates, build_candidates, inventory_legacy
from mote.runtime.models.session_projection import ModelSessionProjectionState, ModelSessionProjectionStore
from mote.runtime.session.timers import SessionTimerState, SessionTimerStore
from mote.runtime.session.workspace import SessionSpace, SessionWorkspace
from mote.runtime.tools.effect_store import ToolEffectState, ToolEffectStore


def _identity() -> ToolInvocationIdentity:
    return ToolInvocationIdentity(
        ToolInvocationId("tool-1"),
        ToolAttemptOrdinal(1),
        "tool/v1",
        1,
        "sha256-" + "0" * 64,
        "agent",
        "run",
    )


def _record(**changes) -> dict[str, object]:
    value: dict[str, object] = {
        "step_id": "tool-1",
        "kind": "tool",
        "effect": "external",
        "status": "started",
        "seq": 0,
        "name": "Curl",
        "tool_call_id": "tool-1",
        "started_at": 1.0,
        "ended_at": None,
        "payload": None,
        "success": True,
        "invocation_identity": _identity().to_payload(),
    }
    value.update(changes)
    return value


def _recovery() -> ModelCallRecovery:
    plan = ModelCallPlannedRecord(
        model_call_id="model-1",
        plan_id="plan",
        route_id="default",
        runtime_generation_id="runtime",
        topology_revision="topology",
        config_revision="config",
        endpoint_ids=("endpoint",),
        budget=AttemptBudget(),
    )
    terminal = ModelCallFinishedRecord(
        model_call_id="model-1",
        state=ModelCallState.SUCCEEDED,
        selected_endpoint_id="endpoint",
        accepted_response=CanonicalModelResponse(output=GenerateOutput(content="canonical")),
    )
    return ModelCallRecovery(
        model_call_id="model-1",
        state=ModelCallState.SUCCEEDED,
        plan=plan,
        original_plan=plan,
        plans=(plan,),
        attempts_started=0,
        attempts_finished=0,
        terminal=terminal,
    )


class _Models:
    def inspect_recovery(self, model_call_id: str) -> ModelRecoveryInspection:
        recovery = _recovery() if model_call_id == "model-1" else None
        return ModelRecoveryInspection(
            model_call_id=model_call_id,
            disposition=(
                ModelRecoveryDisposition.TERMINAL if recovery is not None else ModelRecoveryDisposition.ABSENT
            ),
            recovery=recovery,
        )


def test_run_journal_three_domain_candidate_and_manifest_cutover(tmp_path) -> None:
    workspace = SessionWorkspace(tmp_path)
    ledger = workspace.space("session", SessionSpace.LEDGER)
    ledger.mkdir(parents=True)
    source = ledger / "run-journal.jsonl"
    checkpoint = InferenceCheckpointState("model-1")
    rows = [
        _record(),
        _record(status="completed", ended_at=2.0, payload="receipt"),
        _record(
            step_id="think:1",
            kind="think",
            effect="pure",
            seq=1,
            name="",
            tool_call_id=None,
            payload=json.dumps({"checkpoint": asdict(checkpoint)}),
            invocation_identity=None,
        ),
        _record(
            step_id="timer:1",
            kind="timer",
            effect="pure",
            seq=1,
            name="",
            tool_call_id=None,
            payload=repr(time.time() - 1),
            invocation_identity=None,
        ),
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    inventory = inventory_legacy(source, "session")
    candidates = build_candidates(
        inventory,
        tmp_path / "candidate",
        _Models(),
        now=AbsoluteInstant.from_datetime(datetime.now(timezone.utc)),
    )
    activate_candidates(candidates, source, ledger, expected_source_digest=inventory.source_digest)

    assert source.exists()  # migration evidence, never a production reader
    tool = ToolEffectStore("session", workspace).get("tool-1")
    model = ModelSessionProjectionStore("session", workspace, approved_model_checkpoint_policy()).get("model-1")
    timer = SessionTimerStore("session", workspace).get("timer:1")
    assert tool is not None and tool.state is ToolEffectState.SUCCEEDED
    assert model is not None and model.state is ModelSessionProjectionState.INTENT_COMMITTED
    assert timer is not None and timer.state is SessionTimerState.MISFIRED


def test_legacy_source_without_manifest_blocks_all_production_owners(tmp_path) -> None:
    workspace = SessionWorkspace(tmp_path)
    ledger = workspace.space("session", SessionSpace.LEDGER)
    ledger.mkdir(parents=True)
    (ledger / "run-journal.jsonl").write_text("blocked", encoding="utf-8")
    for build in (
        lambda: ToolEffectStore("session", workspace),
        lambda: ModelSessionProjectionStore("session", workspace, approved_model_checkpoint_policy()),
        lambda: SessionTimerStore("session", workspace),
    ):
        with pytest.raises(RuntimeError, match="cutover"):
            build()
