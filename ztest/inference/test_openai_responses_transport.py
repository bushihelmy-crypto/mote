from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.models.transports.openai import ProviderProtocolError
from mote.product.models.transports.openai_responses import (
    _responses_body,
    _responses_url,
    _responses_usage,
    _validate_completed_response,
)

DIGEST = "sha256:" + "a" * 64


def _request(invocation):
    now = datetime.now(timezone.utc)
    return InferenceAttemptRequest(
        model_call_id="call",
        owner_journal_id="journal",
        attempt_id="attempt",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint=EndpointDescriptor(
            endpoint_id="endpoint",
            transport="openai_responses",
            provider="openai",
            model="gpt",
            base_url_identity="https://api.example.test",
            credential_pool_id="pool",
            lifecycle_revision="1",
        ),
        credential_slot_id="slot",
        credential_version="1",
        invocation=invocation,
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=10),
            remaining_seconds_at_send=10,
            sent_at_utc=now,
        ),
        stream=False,
        principal=InferencePrincipal(
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="1",
            delegation_digest=DIGEST,
        ),
        scheduling=TrustedSchedulingClass(),
    )


def test_responses_url_body_and_usage_are_native():
    assert _responses_url("https://api.example.test") == "https://api.example.test/v1/responses"
    request = _request(
        {
            "input": [{"role": "user", "content": "hello"}],
            "reasoning": {"effort": "high"},
        }
    )
    body = _responses_body(request, stream=True)
    assert body["reasoning"] == {"effort": "high"}
    assert body["stream"] is True
    assert _responses_usage({"usage": {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10}}) == 10


def test_responses_validator_requires_completed_lifecycle():
    _validate_completed_response({"id": "resp", "status": "completed", "output": []})
    with pytest.raises(ProviderProtocolError, match="not completed"):
        _validate_completed_response({"id": "resp", "status": "incomplete", "output": []})
    with pytest.raises(ProviderProtocolError, match="output array"):
        _validate_completed_response({"id": "resp", "status": "completed"})
