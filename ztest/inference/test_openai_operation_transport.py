import asyncio
import hashlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.artifact import ArtifactRef, ArtifactRetention, ArtifactSensitivity, ResolvedArtifact
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.executions import BoundExecutionRequest, TransferPartRequest
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.product.models.transports.openai_operations import OpenAIOperationTransport

DIGEST = "sha256:" + "a" * 64


class _Lifecycle:
    def __init__(self):
        self.started = 0
        self.responses = 0

    async def wire_started(self):
        self.started += 1

    async def response_started(self):
        self.responses += 1


class _Connection:
    def __init__(self, session):
        self.session = session
        self.released = 0

    async def release(self):
        self.released += 1


def _request(operation, payload):
    now = datetime.now(timezone.utc)
    return BoundExecutionRequest(
        execution_id=f"execution-{operation}",
        owner_journal_id="journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="endpoint",
        credential_slot_id="slot",
        credential_version="one",
        operation=operation,
        payload=payload,
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=30),
            remaining_seconds_at_send=30,
            sent_at_utc=now,
        ),
        principal=InferencePrincipal(
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="one",
            delegation_digest=DIGEST,
        ),
        scheduling=TrustedSchedulingClass(),
    )


def test_openai_batch_and_file_list_use_exact_wire_routes():
    async def scenario():
        observed = []

        async def batches(request):
            observed.append(
                (
                    request.method,
                    request.path,
                    dict(request.query),
                    await request.json() if request.can_read_body else None,
                )
            )
            return web.json_response({"object": "batch"})

        async def files(request):
            observed.append((request.method, request.path, dict(request.query), None))
            return web.json_response({"object": "list", "data": []})

        async def resource(request):
            observed.append((request.method, request.raw_path, dict(request.query), None))
            return web.json_response({"id": request.match_info["resource_id"]})

        app = web.Application()
        app.router.add_post("/v1/batches", batches)
        app.router.add_get("/v1/batches", batches)
        app.router.add_get("/v1/files", files)
        app.router.add_get("/v1/batches/{resource_id}", resource)
        app.router.add_post("/v1/batches/{resource_id}/cancel", resource)
        app.router.add_delete("/v1/files/{resource_id}", resource)
        async with TestClient(TestServer(app)) as client, ClientSession() as session:
            connection = _Connection(session)
            transport = OpenAIOperationTransport(
                endpoint_id="endpoint",
                credential_slot_id="slot",
                base_url=str(client.make_url("/")),
                allow_http_for_testing=True,
                connection=connection,
                auth_headers=lambda: _headers(),
            )
            lifecycle = _Lifecycle()
            loop = asyncio.get_running_loop()
            results = [
                await transport.execute_once(
                    _request("batch.create", {"input_file_id": "file-1"}),
                    local_deadline=loop.time() + 10,
                    lifecycle=lifecycle,
                ),
                await transport.execute_once(
                    _request("batch.list", {"limit": 2}),
                    local_deadline=loop.time() + 10,
                    lifecycle=lifecycle,
                ),
                await transport.execute_once(
                    _request("file.list", {"purpose": "batch"}),
                    local_deadline=loop.time() + 10,
                    lifecycle=lifecycle,
                ),
                await transport.execute_once(
                    _request("batch.retrieve", {"batch_id": "batch/one"}),
                    local_deadline=loop.time() + 10,
                    lifecycle=lifecycle,
                ),
                await transport.execute_once(
                    _request("batch.cancel", {"batch_id": "batch-1"}),
                    local_deadline=loop.time() + 10,
                    lifecycle=lifecycle,
                ),
                await transport.execute_once(
                    _request("file.delete", {"file_id": "file-1"}),
                    local_deadline=loop.time() + 10,
                    lifecycle=lifecycle,
                ),
            ]
            return observed, results, lifecycle

    observed, results, lifecycle = asyncio.run(scenario())
    assert observed == [
        ("POST", "/v1/batches", {}, {"input_file_id": "file-1"}),
        ("GET", "/v1/batches", {"limit": "2"}, None),
        ("GET", "/v1/files", {"purpose": "batch"}, None),
        ("GET", "/v1/batches/batch%2Fone", {}, None),
        ("POST", "/v1/batches/batch-1/cancel", {}, None),
        ("DELETE", "/v1/files/file-1", {}, None),
    ]
    assert [result.payload["result"].get("object") for result in results] == [
        "batch",
        "batch",
        "list",
        None,
        None,
        None,
    ]
    assert lifecycle.responses == 6


def test_openai_file_upload_resolves_verified_artifact_bytes():
    async def scenario():
        content = b'{"custom_id":"one"}\n'
        digest = hashlib.sha256(content).hexdigest()
        ref = ArtifactRef(
            artifact_id="artifact-1",
            revision=1,
            representation="original",
            kind="provider_file",
            mime_type="application/jsonl",
            content_ref=f"sha256:{digest}",
            digest=digest,
            size=len(content),
            retention=ArtifactRetention.PROJECT,
            sensitivity=ArtifactSensitivity.PRIVATE,
            suggested_name="input.jsonl",
        )
        observed = {}

        async def upload(request):
            reader = await request.multipart()
            purpose = await (await reader.next()).text()
            file_part = await reader.next()
            observed.update(
                purpose=purpose,
                filename=file_part.filename,
                content_type=file_part.headers["Content-Type"],
                content=await file_part.read(),
            )
            return web.json_response({"id": "file-provider"})

        async def resolve(value):
            assert value == ref
            return ResolvedArtifact(ref=ref, content=content)

        app = web.Application()
        app.router.add_post("/v1/files", upload)
        async with TestClient(TestServer(app)) as client, ClientSession() as session:
            transport = OpenAIOperationTransport(
                endpoint_id="endpoint",
                credential_slot_id="slot",
                base_url=str(client.make_url("/")),
                allow_http_for_testing=True,
                connection=_Connection(session),
                auth_headers=lambda: _headers(),
                artifact_resolver=resolve,
            )
            base = _request("file.upload", {"artifact": asdict(ref), "purpose": "batch"})
            request = TransferPartRequest(
                **base.model_dump(),
                transfer_id="transfer",
                part_number=1,
                offset=0,
                length=len(content),
                content_digest=f"sha256:{digest}",
            )
            result = await transport.execute_once(
                request,
                local_deadline=asyncio.get_running_loop().time() + 10,
                lifecycle=_Lifecycle(),
            )
            return observed, result

    observed, result = asyncio.run(scenario())
    assert observed == {
        "purpose": "batch",
        "filename": "input.jsonl",
        "content_type": "application/jsonl",
        "content": b'{"custom_id":"one"}\n',
    }
    assert result.payload == {"result": {"id": "file-provider"}}


def test_openai_file_content_is_published_as_artifact():
    async def scenario():
        published = []

        async def content(request):
            return web.Response(body=b"provider bytes", content_type="application/octet-stream")

        async def publish(value, content_type, filename):
            published.append((value, content_type, filename))
            digest = hashlib.sha256(value).hexdigest()
            return ArtifactRef(
                artifact_id="download-1",
                revision=1,
                representation="original",
                kind="provider_file",
                mime_type=content_type,
                content_ref=f"sha256:{digest}",
                digest=digest,
                size=len(value),
                suggested_name=filename,
            )

        app = web.Application()
        app.router.add_get("/v1/files/{resource_id}/content", content)
        async with TestClient(TestServer(app)) as client, ClientSession() as session:
            transport = OpenAIOperationTransport(
                endpoint_id="endpoint",
                credential_slot_id="slot",
                base_url=str(client.make_url("/")),
                allow_http_for_testing=True,
                connection=_Connection(session),
                auth_headers=lambda: _headers(),
                artifact_publisher=publish,
            )
            result = await transport.execute_once(
                _request("file.content", {"file_id": "file-1"}),
                local_deadline=asyncio.get_running_loop().time() + 10,
                lifecycle=_Lifecycle(),
            )
            return published, result.payload

    published, payload = asyncio.run(scenario())
    assert published == [(b"provider bytes", "application/octet-stream", "file-1.bin")]
    assert payload["result"]["artifact"]["artifact_id"] == "download-1"


def test_openai_video_container_and_container_file_routes_are_exact():
    from mote.product.models.transports.openai_operations import _command

    assert _command("video.generate", {"prompt": "ocean"}) == ("POST", "/videos", None, {"prompt": "ocean"})
    assert _command("video.retrieve", {"video_id": "video/one"}) == ("GET", "/videos/video%2Fone", None, None)
    assert _command("video.download", {"video_id": "video-1"}) == ("GET", "/videos/video-1/content", None, None)
    assert _command("video.remix", {"video_id": "video/one", "prompt": "sunset"}) == (
        "POST",
        "/videos/video%2Fone/remix",
        None,
        {"prompt": "sunset"},
    )
    assert _command("container.create", {"name": "sandbox"}) == ("POST", "/containers", None, {"name": "sandbox"})
    assert _command("container.delete", {"container_id": "c/1"}) == ("DELETE", "/containers/c%2F1", None, None)
    assert _command("container.list", {"limit": 2}) == ("GET", "/containers", {"limit": "2"}, None)
    assert _command(
        "container_file.create",
        {"container_id": "c-1", "path": "/workspace/a.txt"},
    ) == (
        "POST",
        "/containers/c-1/files",
        None,
        {"path": "/workspace/a.txt"},
    )
    assert _command(
        "container_file.content",
        {"container_id": "c-1", "file_id": "f/1"},
    ) == ("GET", "/containers/c-1/files/f%2F1/content", None, None)


async def _headers():
    return {"Authorization": "Bearer test"}


def test_response_lifecycle_commands_map_to_single_provider_requests():
    from mote.product.models.transports.openai_operations import _command

    assert _command("response.retrieve", {"response_id": "resp_1"}) == ("GET", "/responses/resp_1", {}, None)
    assert _command("response.cancel", {"response_id": "resp_1"}) == ("POST", "/responses/resp_1/cancel", None, None)
    assert _command("response.delete", {"response_id": "resp_1"}) == ("DELETE", "/responses/resp_1", None, None)
    assert _command("response.input_items", {"response_id": "resp_1", "limit": 20}) == (
        "GET",
        "/responses/resp_1/input_items",
        {"limit": "20"},
        None,
    )
