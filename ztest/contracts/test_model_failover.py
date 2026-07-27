from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from mote.contracts.models import (
    AdmissionGate,
    AdmissionVerdict,
    AttemptBudget,
    AttemptState,
    CanonicalMessage,
    EndpointDescriptor,
    FailoverPlan,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    GenerateInput,
    GenerateOutput,
    HealthVerdict,
    ModelAttemptFinishedRecord,
    ModelAttemptStartedRecord,
    ModelCallFinishedRecord,
    ModelCallJournalRecord,
    ModelCallPlannedRecord,
    ModelCallState,
    ModelInvocation,
    ModelOperation,
    OperatorState,
    OperatorTransition,
    RequestRequirements,
    ResolvedModelResponse,
    ResourceIdentity,
    Retryability,
)


def test_model_invocation_round_trip_preserves_discriminators() -> None:
    invocation = ModelInvocation(
        model_call_id="call-1",
        route_id="interactive",
        task="interactive",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(messages=(CanonicalMessage(role="user", content="hello"),)),
    )

    restored = ModelInvocation.model_validate_json(invocation.model_dump_json())

    assert restored == invocation
    assert restored.input.kind == "generate"
    with pytest.raises(ValidationError, match="does not match input kind"):
        ModelInvocation.model_validate({**invocation.model_dump(), "operation": "web_search"})


def test_failover_plan_and_response_are_json_round_trip_stable() -> None:
    endpoint = EndpointDescriptor(
        endpoint_id="primary",
        transport="anthropic",
        provider="anthropic",
        model="claude-sonnet-4-8",
        base_url_identity="https://api.anthropic.com",
        credential_pool_id="anthropic-main",
        lifecycle_revision="rev-1",
    )
    plan = FailoverPlan(
        plan_id="plan-1",
        model_call_id="call-1",
        config_revision="rev-1",
        policy_id="default-v1",
        endpoints=(endpoint,),
        requirements=RequestRequirements(needs_tools=True),
        budget=AttemptBudget(),
    )
    response = ResolvedModelResponse(
        output=GenerateOutput(content="done"),
        cost_usd=Decimal("0.0123"),
        endpoint_id="primary",
        endpoint_fingerprint="endpoint-fp",
        model_or_deployment="claude-sonnet-4-8",
        tenant_fingerprint="tenant-fp",
        credential_slot_id="slot-a",
    )

    assert FailoverPlan.model_validate_json(plan.model_dump_json()) == plan
    assert ResolvedModelResponse.model_validate_json(response.model_dump_json()) == response


def test_attempt_budget_rejects_unreachable_local_limits() -> None:
    with pytest.raises(ValidationError, match="max_endpoint_switches"):
        AttemptBudget(
            max_wire_attempts=2,
            max_attempts_per_endpoint=2,
            max_endpoint_switches=2,
            max_credential_rotations=1,
            max_request_transforms=1,
        )


def test_admission_verdict_round_trip_is_typed_and_secret_opaque() -> None:
    resource = ResourceIdentity(
        endpoint_id="primary",
        transport="openai",
        endpoint_fingerprint="endpoint-fp",
        model_or_deployment="model",
        tenant_fingerprint="tenant-fp",
        credential_slot_id="slot-a",
    )
    verdict = AdmissionVerdict(
        gate=AdmissionGate.CREDENTIAL,
        reason="credential quarantined",
        resource=resource,
        disposition=FailureDisposition(
            reason=FailureReason.AUTH_REJECTED,
            domain=FailureDomain.CREDENTIAL,
            retryability=Retryability.AFTER_CHANGE,
            health_verdict=HealthVerdict.CREDENTIAL_REJECTED,
        ),
    )

    restored = AdmissionVerdict.model_validate_json(verdict.model_dump_json())

    assert restored == verdict
    assert "api-key" not in verdict.model_dump_json()


def test_operator_transition_round_trip_is_revisioned_and_secret_opaque() -> None:
    transition = OperatorTransition(
        resource=ResourceIdentity(
            endpoint_id="primary",
            transport="openai",
            endpoint_fingerprint="endpoint-fp",
            model_or_deployment="model",
            tenant_fingerprint="tenant-fp",
            credential_slot_id="slot-a",
        ),
        previous_state=OperatorState.ENABLED,
        state=OperatorState.DRAINING,
        control_revision=1,
        config_revision="config-1",
        actor="operator:test",
        reason="maintenance",
    )

    restored = OperatorTransition.model_validate_json(transition.model_dump_json())

    assert restored == transition
    assert "api-key" not in transition.model_dump_json()


def test_durable_contract_rejects_non_json_payload_objects() -> None:
    with pytest.raises(ValidationError):
        CanonicalMessage(role="user", content=object())


def test_model_call_journal_records_round_trip_through_discriminated_union() -> None:
    records = (
        ModelCallPlannedRecord(
            model_call_id="call-1",
            plan_id="plan-1",
            route_id="default",
            config_revision="revision-1",
            endpoint_ids=("primary",),
            budget=AttemptBudget(),
        ),
        ModelAttemptStartedRecord(
            model_call_id="call-1",
            attempt_id="call-1:1",
            ordinal=1,
            endpoint_id="primary",
            endpoint_fingerprint="endpoint-fingerprint",
            credential_slot_id="primary:0",
            timeout_seconds=10,
        ),
        ModelAttemptFinishedRecord(
            model_call_id="call-1",
            attempt_id="call-1:1",
            ordinal=1,
            state=AttemptState.SUCCEEDED,
        ),
        ModelCallFinishedRecord(
            model_call_id="call-1",
            state=ModelCallState.SUCCEEDED,
            selected_endpoint_id="primary",
            wire_attempts=1,
        ),
    )
    adapter = TypeAdapter(ModelCallJournalRecord)

    assert tuple(adapter.validate_json(record.model_dump_json()) for record in records) == (records)
