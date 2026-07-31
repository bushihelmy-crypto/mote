import asyncio
from datetime import datetime
from types import SimpleNamespace

from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.wire_permit import WirePermit
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.interfaces.inference_api.composition import build_generation_inference_api

DIGEST = "sha256:" + "a" * 64


class _Gateway:
    def route_profiles(self, route):
        return ()


class _Issuer:
    def issue(self, **values):
        return WirePermit(
            nonce="0123456789abcdef",
            issuer_key_id="key",
            trust_revision=1,
            signature="signature",
            **values,
        )


class _Execution:
    def __init__(self, request):
        self.request = request
        self.events = [
            AttemptLifecycleEvent(
                attempt_id=request.execution_id,
                sequence=1,
                receipt_revision=2,
                generation_id=request.generation_id,
                event_type=AttemptEventType.WIRE_AUTHORIZATION_REQUIRED,
            )
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def authorize_wire(self, permit):
        self.events.append(
            AttemptLifecycleEvent(
                attempt_id=self.request.execution_id,
                sequence=2,
                receipt_revision=3,
                generation_id=self.request.generation_id,
                event_type=AttemptEventType.SUCCEEDED,
                payload={"result": {"operation": self.request.operation}},
            )
        )

    async def cancel(self, reason):
        return None


class _Commands:
    def __init__(self):
        self.requests = []

    async def start_command(self, request):
        self.requests.append(request)
        return _Execution(request)


def test_generation_composition_builds_pinned_command_owner():
    async def scenario():
        commands = _Commands()
        lease = SimpleNamespace(
            gateway=_Gateway(),
            command_runtime=commands,
            session_runtime=None,
            transfer_runtime=None,
            permit_issuer=_Issuer(),
            permit_audience="shared/socket/model/tenant",
            generation_id="generation-1",
            generation_artifact_digest=DIGEST,
            default_model=SimpleNamespace(model="model"),
        )
        app = build_generation_inference_api(
            lease,
            bearer_token="secret",
            endpoint=EndpointDescriptor(
                endpoint_id="endpoint",
                transport="openai",
                provider="openai",
                model="model",
                base_url_identity="https://example.test",
                credential_pool_id="pool",
                lifecycle_revision="one",
            ),
            credential_slot_id="slot",
            credential_version="one",
        )
        owner = next(
            value for key, value in app.items() if getattr(key, "_name", "").endswith(".inference_durable_operations")
        )
        response_owner = next(
            value for key, value in app.items() if getattr(key, "_name", "").endswith(".inference_durable_responses")
        )
        result = await owner.execute("batches", {"input_file_id": "file"})
        response = await response_owner.retrieve("resp_1")
        return result, response, commands.requests

    result, response, requests = asyncio.run(scenario())
    assert result == {"operation": "batch.create"}
    assert response == {"operation": "response.retrieve"}
    request = requests[0]
    assert request.generation_id == "generation-1"
    assert request.generation_artifact_digest == DIGEST
    assert request.credential_slot_id == "slot"
    assert request.operation == "batch.create"
    assert isinstance(request.deadline.sent_at_utc, datetime)
    assert requests[1].operation == "response.retrieve"
    assert requests[1].payload == {"response_id": "resp_1"}


def test_generation_composition_fails_closed_without_runtime_owners():
    lease = SimpleNamespace(
        gateway=_Gateway(),
        command_runtime=None,
        session_runtime=None,
        transfer_runtime=None,
        permit_issuer=_Issuer(),
        permit_audience="audience",
        generation_id="generation-1",
        generation_artifact_digest=DIGEST,
        default_model=SimpleNamespace(model="model"),
    )
    app = build_generation_inference_api(
        lease,
        bearer_token="secret",
        endpoint=EndpointDescriptor(
            endpoint_id="endpoint",
            transport="openai",
            provider="openai",
            model="model",
            base_url_identity="https://example.test",
            credential_pool_id="pool",
            lifecycle_revision="one",
        ),
        credential_slot_id="slot",
        credential_version="one",
    )
    names = {getattr(key, "_name", "").rsplit(".", 1)[-1] for key in app}
    assert "inference_durable_operations" not in names
    assert "inference_durable_responses" not in names
    assert "inference_realtime_sessions" not in names
    assert "inference_artifact_operations" not in names
