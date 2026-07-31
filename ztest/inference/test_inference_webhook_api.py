import asyncio

from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.inference.provider_evidence import ProviderEvidenceConflictError
from mote.product.interfaces.inference_webhook_api import build_inference_webhook_api


def test_provider_webhook_verifies_raw_body_and_deduplicates_at_sink():
    class Verifier:
        async def verify(self, provider, event_id, signature, body):
            return provider == "openai" and event_id == "event-1" and signature == "signed"

    class Sink:
        def __init__(self):
            self.events = {}

        async def append(self, evidence):
            inserted = evidence.event_id not in self.events
            self.events[evidence.event_id] = evidence
            return inserted

    async def scenario():
        sink = Sink()
        app = build_inference_webhook_api(Verifier(), sink)
        body = {
            "execution_id": "execution",
            "event_type": "completed",
            "occurred_at": "2026-07-30T00:00:00Z",
        }
        headers = {
            "X-Provider-Event-Id": "event-1",
            "X-Provider-Signature": "signed",
        }
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/v1/webhooks/provider/openai", json=body)).status == 401
            assert (await client.post("/v1/webhooks/provider/openai", json=body, headers=headers)).status == 202
            assert (await client.post("/v1/webhooks/provider/openai", json=body, headers=headers)).status == 200
        return sink

    sink = asyncio.run(scenario())
    assert sink.events["event-1"].execution_id == "execution"


def test_provider_webhook_reports_durable_evidence_conflict():
    class Verifier:
        async def verify(self, provider, event_id, signature, body):
            return True

    class Sink:
        async def append(self, evidence):
            raise ProviderEvidenceConflictError("identity conflict")

    async def scenario():
        app = build_inference_webhook_api(Verifier(), Sink())
        body = {
            "execution_id": "execution",
            "event_type": "completed",
            "occurred_at": "2026-07-30T00:00:00Z",
        }
        headers = {
            "X-Provider-Event-Id": "event-1",
            "X-Provider-Signature": "signed",
        }
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/v1/webhooks/provider/openai", json=body, headers=headers)
            return response.status, await response.json()

    assert asyncio.run(scenario()) == (409, {"error": "evidence_conflict"})
