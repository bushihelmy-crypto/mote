import asyncio

import pytest

from mote.contracts.model.failover import CredentialVerdict, QuotaObservation
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionPool, ConnectionConfig
from mote.product.models.transports.openai import (
    ProviderProtocolError,
    _chat_completions_url,
    _decode_json,
    _validate_status_and_payload,
    _validated_headers,
)


def test_openai_url_and_headers_are_strict():
    assert _chat_completions_url("https://api.example.test") == "https://api.example.test/v1/chat/completions"
    assert _chat_completions_url("https://api.example.test/v1") == "https://api.example.test/v1/chat/completions"
    with pytest.raises(ValueError, match="HTTPS"):
        _chat_completions_url("http://api.example.test")
    with pytest.raises(ValueError, match="credential-free"):
        _chat_completions_url("https://secret@api.example.test")
    assert _validated_headers({"Authorization": "Bearer opaque"})["Authorization"] == "Bearer opaque"
    with pytest.raises(ValueError, match="forbidden"):
        _validated_headers({"Authorization": "x", "Connection": "keep-alive"})


def test_response_validator_rejects_html_malformed_and_error_in_200():
    with pytest.raises(ProviderProtocolError, match="HTML"):
        _decode_json(b"<html>failure</html>")
    with pytest.raises(ProviderProtocolError, match="malformed"):
        _decode_json(b"{")
    with pytest.raises(ProviderProtocolError, match="error envelope"):
        _validate_status_and_payload(200, {"error": {"message": "bad"}})
    with pytest.raises(ProviderProtocolError, match="status 503"):
        _validate_status_and_payload(503, {"message": "unavailable"})


def test_response_validator_emits_owned_governance_verdicts():
    with pytest.raises(ProviderProtocolError) as auth:
        _validate_status_and_payload(401, {"error": {}})
    assert auth.value.disposition.credential_verdict is CredentialVerdict.QUARANTINE

    with pytest.raises(ProviderProtocolError) as quota:
        _validate_status_and_payload(429, {"error": {}}, retry_after_seconds=3)
    assert quota.value.disposition.quota_observation is QuotaObservation.RETRY_AFTER
    assert quota.value.retry_after_seconds == 3


def test_connection_pool_reuses_policy_fingerprint_until_last_generation_lease():
    async def scenario():
        pool = AioHttpConnectionPool()
        config = ConnectionConfig(fingerprint="https-example-policy-v1", connection_limit=2)
        first = await pool.acquire(config)
        second = await pool.acquire(config)
        assert first.session is second.session
        session = first.session
        await first.release()
        assert not session.closed
        await second.release()
        assert session.closed
        await pool.aclose()

    asyncio.run(scenario())
