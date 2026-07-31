import asyncio
import base64
import hashlib
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.artifact import ArtifactRef
from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.models.transports.google_finite import GoogleFiniteTransport

DIGEST = "sha256:" + "c" * 64


class _Connection:
    def __init__(self, session):
        self.session = session

    async def release(self):
        return None


class _Lifecycle:
    async def wire_started(self):
        return None

    async def response_started(self):
        return None


def _request(operation, value, base_url):
    now = datetime.now(timezone.utc)
    return InferenceAttemptRequest(
        model_call_id="call",
        owner_journal_id="journal",
        attempt_id=operation,
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint=EndpointDescriptor(
            endpoint_id="endpoint",
            transport="gemini",
            provider="google",
            model="model",
            base_url_identity=base_url,
            credential_pool_id="pool",
            lifecycle_revision="one",
        ),
        credential_slot_id="slot",
        credential_version="one",
        invocation={"operation": operation, "input": value},
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=30),
            remaining_seconds_at_send=30,
            sent_at_utc=now,
        ),
        stream=False,
        principal=InferencePrincipal(
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="one",
            delegation_digest=DIGEST,
        ),
        scheduling=TrustedSchedulingClass(),
    )


def test_google_embedding_and_image_generation_translate_canonical_results():
    async def scenario():
        bodies = []
        published = []

        async def embedding(request):
            bodies.append(await request.json())
            return web.json_response({"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3]}]})

        async def image(request):
            bodies.append(await request.json())
            return web.json_response(
                {
                    "predictions": [
                        {
                            "bytesBase64Encoded": base64.b64encode(b"png").decode(),
                            "mimeType": "image/png",
                        }
                    ]
                }
            )

        async def publish(content, mime_type, filename):
            published.append((content, mime_type, filename))
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(
                artifact_id="image-1",
                revision=1,
                representation="original",
                kind="provider_file",
                mime_type=mime_type,
                content_ref=f"sha256:{digest}",
                digest=digest,
                size=len(content),
                suggested_name=filename,
            )

        app = web.Application()
        app.router.add_post("/v1beta/models/model:batchEmbedContents", embedding)
        app.router.add_post("/v1beta/models/model:predict", image)
        async with TestClient(TestServer(app)) as client, ClientSession() as session:
            base_url = str(client.make_url("/"))
            transport = GoogleFiniteTransport(
                base_url=base_url,
                connection=_Connection(session),
                auth_headers=_headers,
                artifact_publisher=publish,
                allow_http_for_testing=True,
            )
            loop = asyncio.get_running_loop()
            embedded = await transport.generate_once(
                _request(
                    "embedding",
                    {
                        "kind": "embedding",
                        "values": ["one", "two"],
                        "dimensions": 2,
                    },
                    base_url,
                ),
                local_deadline=loop.time() + 10,
                lifecycle=_Lifecycle(),
                stream=None,
            )
            generated = await transport.generate_once(
                _request(
                    "image_generation",
                    {
                        "kind": "image_generation",
                        "prompt": "tree",
                        "options": {"sampleCount": 1},
                    },
                    base_url,
                ),
                local_deadline=loop.time() + 10,
                lifecycle=_Lifecycle(),
                stream=None,
            )
            return bodies, published, embedded.payload, generated.payload

    bodies, published, embedded, generated = asyncio.run(scenario())
    assert bodies[0]["requests"][0]["outputDimensionality"] == 2
    assert bodies[1] == {
        "instances": [{"prompt": "tree"}],
        "parameters": {"sampleCount": 1},
    }
    assert embedded["output"]["vectors"] == [[0.1, 0.2], [0.3]]
    assert generated["output"]["artifacts"][0]["artifact_id"] == "image-1"
    assert published == [(b"png", "image/png", "generated-1.png")]


async def _headers():
    return {"x-goog-api-key": "secret"}
