from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.models.transports.google import (
    _generate_content_body,
    _generate_content_url,
    _google_headers,
    _google_usage,
    _validate_generate_content,
)
from mote.product.models.transports.openai import ProviderProtocolError

DIGEST = "sha256:" + "c" * 64


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
            transport="gemini_generate_content",
            provider="google",
            model="gemini-pro",
            base_url_identity="https://generativelanguage.googleapis.test/v1beta",
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


def test_google_url_auth_and_native_body():
    assert _generate_content_url(
        "https://generativelanguage.googleapis.test/v1beta",
        "gemini-pro",
        stream=True,
    ).endswith("/models/gemini-pro:streamGenerateContent?alt=sse")
    assert _google_headers({"x-goog-api-key": "secret"})["content-type"] == "application/json"
    body = _generate_content_body(
        _request(
            {
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                "cachedContent": "cachedContents/one",
            }
        )
    )
    assert body["cachedContent"] == "cachedContents/one"


def test_google_validator_requires_candidates_and_terminal_reason():
    payload = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"totalTokenCount": 12},
    }
    _validate_generate_content(payload, require_terminal=True)
    assert _google_usage(payload) == 12
    with pytest.raises(ProviderProtocolError, match="terminal candidate"):
        _validate_generate_content({"candidates": [{}]}, require_terminal=True)
    with pytest.raises(ProviderProtocolError, match="candidates array"):
        _validate_generate_content({}, require_terminal=False)
