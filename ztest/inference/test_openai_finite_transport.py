import asyncio
import hashlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.artifact import ArtifactRef, ResolvedArtifact
from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.models.transports.openai_finite import OpenAIFiniteTransport

DIGEST = "sha256:" + "a" * 64


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


def _request(operation, value):
    now = datetime.now(timezone.utc)
    return InferenceAttemptRequest(
        model_call_id="call",
        owner_journal_id="journal",
        attempt_id=operation,
        generation_id="generation",
        generation_artifact_digest=DIGEST,
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


def test_openai_embedding_and_image_generation_translate_canonical_results():
    async def scenario():
        bodies = []

        async def embeddings(request):
            bodies.append(await request.json())
            return web.json_response(
                {
                    "id": "embedding-1",
                    "data": [
                        {"index": 1, "embedding": [0.3]},
                        {"index": 0, "embedding": [0.1, 0.2]},
                    ],
                    "usage": {"prompt_tokens": 2, "total_tokens": 2},
                }
            )

        async def images(request):
            bodies.append(await request.json())
            return web.json_response(
                {
                    "id": "image-1",
                    "data": [{"url": "https://example.invalid/image"}],
                    "usage": {"total_tokens": 3},
                }
            )

        app = web.Application()
        app.router.add_post("/v1/embeddings", embeddings)
        app.router.add_post("/v1/images/generations", images)
        async with TestClient(TestServer(app)) as client, ClientSession() as session:
            transport = OpenAIFiniteTransport(
                base_url=str(client.make_url("/")),
                connection=_Connection(session),
                auth_headers=_headers,
                allow_http_for_testing=True,
            )
            loop = asyncio.get_running_loop()
            embedding = await transport.generate_once(
                _request(
                    "embedding",
                    {
                        "kind": "embedding",
                        "values": ["one", "two"],
                        "dimensions": 2,
                    },
                ),
                local_deadline=loop.time() + 10,
                lifecycle=_Lifecycle(),
                stream=None,
            )
            image = await transport.generate_once(
                _request(
                    "image_generation",
                    {
                        "kind": "image_generation",
                        "prompt": "tree",
                        "options": {"size": "1024x1024"},
                    },
                ),
                local_deadline=loop.time() + 10,
                lifecycle=_Lifecycle(),
                stream=None,
            )
            return bodies, embedding.payload, image.payload

    bodies, embedding, image = asyncio.run(scenario())
    assert bodies == [
        {"model": "model", "input": ["one", "two"], "dimensions": 2},
        {"model": "model", "prompt": "tree", "size": "1024x1024"},
    ]
    assert embedding["output"]["vectors"] == [[0.1, 0.2], [0.3]]
    assert embedding["usage"]["input_tokens"] == 2
    assert image["output"]["provider_items"][0]["url"].endswith("/image")


def test_openai_speech_and_transcription_cross_artifact_boundary():
    async def scenario():
        source = b"audio input"
        source_digest = hashlib.sha256(source).hexdigest()
        source_ref = ArtifactRef(
            artifact_id="audio-input",
            revision=1,
            representation="original",
            kind="audio",
            mime_type="audio/wav",
            content_ref=f"sha256:{source_digest}",
            digest=source_digest,
            size=len(source),
            suggested_name="input.wav",
        )
        published = []
        observed = {}

        async def speech(request):
            observed["speech"] = await request.json()
            return web.Response(body=b"mp3 bytes", content_type="audio/mpeg")

        async def transcription(request):
            reader = await request.multipart()
            file_part = await reader.next()
            filename = file_part.filename
            file_content = await file_part.read()
            model_part = await reader.next()
            observed["transcription"] = (filename, file_content, await model_part.text())
            return web.json_response({"id": "transcription-1", "text": "hello"})

        async def resolve(ref):
            assert ref == source_ref
            return ResolvedArtifact(ref=ref, content=source)

        async def publish(content, content_type, filename):
            published.append((content, content_type, filename))
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(
                artifact_id="speech-output",
                revision=1,
                representation="original",
                kind="audio",
                mime_type=content_type,
                content_ref=f"sha256:{digest}",
                digest=digest,
                size=len(content),
                suggested_name=filename,
            )

        app = web.Application()
        app.router.add_post("/v1/audio/speech", speech)
        app.router.add_post("/v1/audio/transcriptions", transcription)
        async with TestClient(TestServer(app)) as client, ClientSession() as session:
            transport = OpenAIFiniteTransport(
                base_url=str(client.make_url("/")),
                connection=_Connection(session),
                auth_headers=_headers,
                allow_http_for_testing=True,
                artifact_resolver=resolve,
                artifact_publisher=publish,
            )
            loop = asyncio.get_running_loop()
            speech_result = await transport.generate_once(
                _request(
                    "speech",
                    {
                        "kind": "speech",
                        "text": "hello",
                        "voice": "alloy",
                        "options": {"format": "mp3"},
                    },
                ),
                local_deadline=loop.time() + 10,
                lifecycle=_Lifecycle(),
                stream=None,
            )
            transcription_result = await transport.generate_once(
                _request(
                    "transcription",
                    {
                        "kind": "transcription",
                        "artifact": asdict(source_ref),
                        "options": {},
                    },
                ),
                local_deadline=loop.time() + 10,
                lifecycle=_Lifecycle(),
                stream=None,
            )
            return observed, published, speech_result.payload, transcription_result.payload

    observed, published, speech, transcription = asyncio.run(scenario())
    assert observed["speech"] == {
        "model": "model",
        "input": "hello",
        "voice": "alloy",
        "format": "mp3",
    }
    assert observed["transcription"] == ("input.wav", b"audio input", "model")
    assert published == [(b"mp3 bytes", "audio/mpeg", "speech.mp3")]
    assert speech["output"]["artifact"]["artifact_id"] == "speech-output"
    assert transcription["output"]["text"] == "hello"


async def _headers():
    return {"Authorization": "Bearer test"}
