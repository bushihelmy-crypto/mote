from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.models.transports.anthropic import (
    _anthropic_body,
    _anthropic_headers,
    _message_usage,
    _messages_url,
    _validate_message,
)
from mote.product.models.transports.openai import ProviderProtocolError

DIGEST = "sha256:" + "b" * 64


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
            transport="anthropic_messages",
            provider="anthropic",
            model="claude",
            base_url_identity="https://api.anthropic.test",
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


def test_anthropic_url_headers_and_native_body():
    assert _messages_url("https://api.anthropic.test") == "https://api.anthropic.test/v1/messages"
    headers = _anthropic_headers({"x-api-key": "secret"}, anthropic_version="2023-06-01")
    assert headers["anthropic-version"] == "2023-06-01"
    request = _request(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 50},
        }
    )
    body = _anthropic_body(request, stream=True)
    assert body["thinking"]["type"] == "enabled"
    assert body["stream"] is True
    with pytest.raises(ValueError, match="positive max_tokens"):
        _anthropic_body(_request({"messages": []}), stream=False)


def test_anthropic_message_validator_and_usage():
    payload = {
        "id": "msg",
        "type": "message",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }
    _validate_message(payload)
    assert _message_usage(payload) == 12
    with pytest.raises(ProviderProtocolError, match="content array"):
        _validate_message({"id": "msg", "type": "message", "usage": {}})
