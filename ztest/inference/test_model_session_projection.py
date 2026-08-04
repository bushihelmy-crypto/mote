from __future__ import annotations

import pytest

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
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.product.models.artifacts import ProductInferenceArtifacts
from mote.runtime.durable.inference_checkpoint import InferenceCheckpoint
from mote.runtime.models.session_projection import ModelSessionProjectionState, ModelSessionProjectionStore
from mote.runtime.session.workspace import SessionWorkspace


def _recovery(call_id: str) -> ModelCallRecovery:
    plan = ModelCallPlannedRecord(
        model_call_id=call_id,
        plan_id="plan",
        route_id="default",
        runtime_generation_id="runtime",
        topology_revision="topology",
        config_revision="config",
        endpoint_ids=("endpoint",),
        budget=AttemptBudget(),
    )
    terminal = ModelCallFinishedRecord(
        model_call_id=call_id,
        state=ModelCallState.SUCCEEDED,
        selected_endpoint_id="endpoint",
        accepted_response=CanonicalModelResponse(output=GenerateOutput(content="paid result")),
    )
    return ModelCallRecovery(
        model_call_id=call_id,
        state=ModelCallState.SUCCEEDED,
        plan=plan,
        original_plan=plan,
        plans=(plan,),
        attempts_started=0,
        attempts_finished=0,
        terminal=terminal,
    )


class _Query:
    def __init__(
        self,
        recovery: ModelCallRecovery | None,
        disposition: ModelRecoveryDisposition | None = None,
    ) -> None:
        self.recovery = recovery
        self.disposition = disposition

    def inspect_recovery(self, model_call_id: str) -> ModelRecoveryInspection:
        recovery = self.recovery if self.recovery is not None and self.recovery.model_call_id == model_call_id else None
        disposition = self.disposition or (
            ModelRecoveryDisposition.TERMINAL if recovery is not None else ModelRecoveryDisposition.ABSENT
        )
        return ModelRecoveryInspection(model_call_id, disposition, recovery)


class _Engine:
    def __init__(self) -> None:
        self.result = None

    def reinstate(self, result) -> None:
        self.result = result


class _Resolver:
    async def resolve(self, ref, policy):
        raise AssertionError("inline Model output must not resolve an Artifact")


@pytest.mark.asyncio
async def test_terminal_model_fact_reinstates_and_acknowledges_without_repay(
    tmp_path,
) -> None:
    workspace = SessionWorkspace(tmp_path)
    store = ModelSessionProjectionStore("session", workspace, approved_model_checkpoint_policy())
    state = InferenceCheckpointState("call")
    store.begin(state)
    engine = _Engine()
    checkpoint = InferenceCheckpoint(
        projections=store,
        model_calls=_Query(_recovery("call")),
        inference_engine=engine,
        artifact_resolver=_Resolver(),
    )

    assert await checkpoint.reinstate() is True
    assert engine.result.content == "paid result"
    assert store.get("call").state is ModelSessionProjectionState.INTENT_COMMITTED
    checkpoint.discard()
    assert store.get("call").state is ModelSessionProjectionState.ACKNOWLEDGED


def test_interrupted_model_call_resumes_same_identity(tmp_path) -> None:
    store = ModelSessionProjectionStore("session", SessionWorkspace(tmp_path), approved_model_checkpoint_policy())
    store.begin(InferenceCheckpointState("call", request_fingerprint="request"))
    checkpoint = InferenceCheckpoint(
        projections=store,
        model_calls=_Query(None),
        inference_engine=_Engine(),
        artifact_resolver=_Resolver(),
    )
    resumed = checkpoint.resume()
    assert resumed is not None
    assert resumed.model_call_id == "call"
    assert resumed.request_fingerprint == "request"


def test_absent_evidence_after_wire_start_fails_closed(tmp_path) -> None:
    store = ModelSessionProjectionStore("session", SessionWorkspace(tmp_path), approved_model_checkpoint_policy())
    checkpoint = InferenceCheckpoint(
        projections=store,
        model_calls=_Query(None),
        inference_engine=_Engine(),
        artifact_resolver=_Resolver(),
    )
    checkpoint.begin_call(InferenceCheckpointState("call", request_fingerprint="request"))
    checkpoint.mark_wire_started()

    recovered = InferenceCheckpoint(
        projections=ModelSessionProjectionStore(
            "session", SessionWorkspace(tmp_path), approved_model_checkpoint_policy()
        ),
        model_calls=_Query(None),
        inference_engine=_Engine(),
        artifact_resolver=_Resolver(),
    )
    with pytest.raises(RuntimeError, match="only recoverable before wire"):
        recovered.resume()


@pytest.mark.parametrize(
    "disposition",
    [
        ModelRecoveryDisposition.CORRUPT,
        ModelRecoveryDisposition.UNSUPPORTED,
        ModelRecoveryDisposition.LEGACY,
        ModelRecoveryDisposition.IDENTITY_MISMATCH,
    ],
)
def test_invalid_recovery_evidence_requires_owner_action(tmp_path, disposition) -> None:
    store = ModelSessionProjectionStore("session", SessionWorkspace(tmp_path), approved_model_checkpoint_policy())
    store.begin(InferenceCheckpointState("call"))
    checkpoint = InferenceCheckpoint(
        projections=store,
        model_calls=_Query(None, disposition),
        inference_engine=_Engine(),
        artifact_resolver=_Resolver(),
    )

    with pytest.raises(RuntimeError, match=f"failed closed: {disposition.value}"):
        checkpoint.resume()

    assert store.get("call").state is ModelSessionProjectionState.OWNER_ACTION_REQUIRED


def test_in_doubt_recovery_is_never_resumed_or_repaid(tmp_path) -> None:
    recovery = _recovery("call").model_copy(update={"state": ModelCallState.IN_DOUBT, "terminal": None})
    store = ModelSessionProjectionStore("session", SessionWorkspace(tmp_path), approved_model_checkpoint_policy())
    store.begin(InferenceCheckpointState("call"))
    checkpoint = InferenceCheckpoint(
        projections=store,
        model_calls=_Query(recovery, ModelRecoveryDisposition.IN_DOUBT),
        inference_engine=_Engine(),
        artifact_resolver=_Resolver(),
    )

    with pytest.raises(RuntimeError, match="failed closed: in_doubt"):
        checkpoint.resume()

    assert store.get("call").state is ModelSessionProjectionState.OWNER_ACTION_REQUIRED


@pytest.mark.asyncio
async def test_oversized_terminal_content_rehydrates_from_canonical_artifact(tmp_path) -> None:
    artifacts = ProductInferenceArtifacts(tmp_path / "artifacts")
    content = "x" * (64 * 1024 + 1)
    ref = await artifacts.publish(content.encode(), "text/plain", "model-response.txt")
    recovery = _recovery("call")
    terminal = recovery.terminal
    assert terminal is not None and terminal.accepted_response is not None
    externalized = GenerateOutput(content_artifact=ref)
    recovery = recovery.model_copy(
        update={
            "terminal": terminal.model_copy(
                update={"accepted_response": terminal.accepted_response.model_copy(update={"output": externalized})}
            )
        }
    )

    class _ArtifactResolver:
        async def resolve(self, artifact_ref, policy):
            assert policy.max_bytes == len(content.encode())
            return await artifacts.resolve(artifact_ref)

    store = ModelSessionProjectionStore("session", SessionWorkspace(tmp_path), approved_model_checkpoint_policy())
    store.begin(InferenceCheckpointState("call"))
    engine = _Engine()
    checkpoint = InferenceCheckpoint(
        projections=store,
        model_calls=_Query(recovery),
        inference_engine=engine,
        artifact_resolver=_ArtifactResolver(),
    )

    assert await checkpoint.reinstate()
    assert engine.result.content == content
