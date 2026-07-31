import asyncio
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.artifact import ArtifactRef
from mote.contracts.model.invocation import (
    EmbeddingOutput,
    ImageGenerationOutput,
    ModelUsage,
    ResolvedModelResponse,
    SpeechOutput,
    TranscriptionOutput,
)
from mote.product.interfaces.inference_api import ModelGatewayCompatibilityOwner, build_inference_api


def _artifact():
    return ArtifactRef(
        artifact_id="artifact-1",
        revision=1,
        representation="original",
        kind="audio",
        mime_type="audio/mpeg",
        content_ref="artifact:audio-1",
        digest="a" * 64,
        size=3,
    )


class _Gateway:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def execute(self, invocation, **options):
        self.calls.append((invocation, options))
        return ResolvedModelResponse(
            output=next(self.outputs),
            usage=ModelUsage(input_tokens=2, total_tokens=2),
            cost_usd=Decimal("0"),
            endpoint_id="endpoint",
            endpoint_fingerprint="fingerprint",
            model_or_deployment="model",
            tenant_fingerprint="tenant",
            credential_slot_id="slot",
            model_call_id=invocation.model_call_id,
        )


def test_model_gateway_owner_projects_all_finite_operation_contracts():
    async def scenario():
        artifact = _artifact()
        gateway = _Gateway(
            [
                EmbeddingOutput(vectors=((0.1, 0.2),)),
                ImageGenerationOutput(provider_items=({"url": "https://example.invalid/image"},)),
                SpeechOutput(artifact=artifact),
                TranscriptionOutput(text="hello"),
            ]
        )
        owner = ModelGatewayCompatibilityOwner(gateway)
        results = [
            await owner.execute("embeddings.create", {"input": "hello"}),
            await owner.execute("images.generate", {"prompt": "a tree"}),
            await owner.execute("audio.speech", {"input": "hello", "voice": "alloy"}),
            await owner.execute(
                "audio.transcriptions",
                {
                    "artifact": {
                        "artifact_id": artifact.artifact_id,
                        "revision": artifact.revision,
                        "representation": artifact.representation,
                        "kind": artifact.kind,
                        "mime_type": artifact.mime_type,
                        "content_ref": artifact.content_ref,
                        "digest": artifact.digest,
                        "size": artifact.size,
                        "retention": artifact.retention.value,
                        "sensitivity": artifact.sensitivity.value,
                        "suggested_name": "",
                    }
                },
            ),
        ]
        return gateway, results

    gateway, results = asyncio.run(scenario())
    assert results[0]["data"][0]["embedding"] == [0.1, 0.2]
    assert results[1]["data"][0]["url"] == "https://example.invalid/image"
    assert results[2]["artifact"]["artifact_id"] == "artifact-1"
    assert results[3]["text"] == "hello"
    assert [call.operation.value for call, _ in gateway.calls] == [
        "embedding",
        "image_generation",
        "speech",
        "transcription",
    ]
    assert all(options == {"stream": False} for _call, options in gateway.calls)


def test_model_gateway_owner_rejects_invalid_input_before_wire():
    async def scenario():
        gateway = _Gateway([])
        owner = ModelGatewayCompatibilityOwner(gateway)
        with pytest.raises(ValueError, match="embedding input"):
            await owner.execute("embeddings.create", {"input": []})
        return gateway.calls

    assert asyncio.run(scenario()) == []


def test_inference_api_defaults_finite_operations_to_model_gateway_owner():
    async def scenario():
        gateway = _Gateway([EmbeddingOutput(vectors=((1.0,),))])
        app = build_inference_api(gateway, bearer_token="secret")
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer secret"},
                json={"input": "hello"},
            )
            return response.status, await response.json(), gateway.calls

    status, document, calls = asyncio.run(scenario())
    assert status == 200
    assert document["data"][0]["embedding"] == [1.0]
    assert len(calls) == 1
