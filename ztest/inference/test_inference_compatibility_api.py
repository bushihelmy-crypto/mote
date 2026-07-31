import asyncio
from decimal import Decimal

from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.model.invocation import GenerateOutput, ModelUsage, ResolvedModelResponse
from mote.contracts.model.topology import DefaultRoute
from mote.product.interfaces.inference_api import build_inference_api


class Gateway:
    def __init__(self):
        self.calls = []

    def route_profiles(self, route):
        return ()

    async def execute(self, invocation, **kwargs):
        self.calls.append((invocation, kwargs))
        return ResolvedModelResponse(
            output=GenerateOutput(content="hello"),
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            cost_usd=Decimal("0"),
            endpoint_id="endpoint",
            endpoint_fingerprint="fingerprint",
            model_or_deployment="model",
            tenant_fingerprint="tenant",
            credential_slot_id="slot",
            model_call_id=invocation.model_call_id,
        )


def test_chat_compatibility_delegates_to_single_runtime_gateway():
    async def scenario():
        gateway = Gateway()
        async with TestClient(TestServer(build_inference_api(gateway, bearer_token="secret"))) as client:
            unauthorized = await client.post("/v1/chat/completions", json={})
            assert unauthorized.status == 401
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer secret"},
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status == 200
            assert (await response.json())["choices"][0]["message"]["content"] == "hello"
        return gateway

    gateway = asyncio.run(scenario())
    assert len(gateway.calls) == 1
    invocation, options = gateway.calls[0]
    assert invocation.route_id == DefaultRoute()
    assert invocation.input.messages[0].content == "hi"
    assert options == {"stream": False}


def test_stream_request_fails_before_gateway_execution():
    async def scenario():
        gateway = Gateway()
        async with TestClient(TestServer(build_inference_api(gateway, bearer_token="secret"))) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer secret"},
                json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status == 400
        return gateway

    gateway = asyncio.run(scenario())
    assert gateway.calls == []


def test_models_requires_authentication_and_reads_gateway_snapshot():
    async def scenario():
        gateway = Gateway()
        async with TestClient(TestServer(build_inference_api(gateway, bearer_token="secret"))) as client:
            denied = await client.get("/v1/models")
            assert denied.status == 401
            response = await client.get("/v1/models", headers={"Authorization": "Bearer secret"})
            assert response.status == 200
            assert await response.json() == {"object": "list", "data": []}

    asyncio.run(scenario())


def test_responses_unary_delegates_without_private_response_storage():
    async def scenario():
        gateway = Gateway()
        async with TestClient(TestServer(build_inference_api(gateway, bearer_token="secret"))) as client:
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer secret"},
                json={
                    "instructions": "Be concise",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        }
                    ],
                },
            )
            assert response.status == 200
            document = await response.json()
            assert document["object"] == "response"
            assert document["status"] == "completed"
            assert document["output"][0]["content"][0]["text"] == "hello"
        return gateway

    gateway = asyncio.run(scenario())
    invocation, options = gateway.calls[0]
    assert invocation.task == "compatibility.responses"
    assert invocation.input.system_prompt == "Be concise"
    assert invocation.input.messages[0].content == "hi"
    assert options == {"stream": False}


def test_response_lifecycle_delegates_to_durable_owner():
    class Owner:
        def __init__(self):
            self.calls = []

        async def retrieve(self, response_id):
            self.calls.append(("retrieve", response_id))
            return {"id": response_id, "status": "completed"}

        async def cancel(self, response_id):
            self.calls.append(("cancel", response_id))
            return {"id": response_id, "status": "cancelled"}

        async def delete(self, response_id):
            self.calls.append(("delete", response_id))
            return {"id": response_id, "deleted": True}

        async def input_items(self, response_id, query):
            self.calls.append(("input_items", response_id, query))
            return {"object": "list", "data": []}

    async def scenario():
        owner = Owner()
        app = build_inference_api(Gateway(), bearer_token="secret", durable_responses=owner)
        headers = {"Authorization": "Bearer secret"}
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/v1/responses/r1", headers=headers)).status == 200
            assert (await client.post("/v1/responses/r1/cancel", headers=headers)).status == 200
            assert (await client.delete("/v1/responses/r1", headers=headers)).status == 200
            assert (await client.get("/v1/responses/r1/input_items?limit=10", headers=headers)).status == 200
        return owner.calls

    assert asyncio.run(scenario()) == [
        ("retrieve", "r1"),
        ("cancel", "r1"),
        ("delete", "r1"),
        ("input_items", "r1", {"limit": "10"}),
    ]
