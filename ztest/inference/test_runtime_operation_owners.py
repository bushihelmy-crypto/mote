import asyncio
import hashlib

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from mote.contracts.artifact import ArtifactRef, ArtifactRevision, ResolvedArtifact
from mote.product.interfaces.inference_api import (
    ArtifactTransferCompatibilityOwner,
    CommandCompatibilityOwner,
    build_inference_api,
)


class _Command:
    def __init__(self):
        self.calls = []

    async def execute(self, operation, payload):
        self.calls.append((operation, payload))
        if operation == "file.content":
            digest = hashlib.sha256(b"download").hexdigest()
            return {
                "artifact": {
                    "artifact_id": "download-1",
                    "revision": 1,
                    "representation": "original",
                    "kind": "provider_file",
                    "mime_type": "application/octet-stream",
                    "content_ref": f"sha256:{digest}",
                    "digest": digest,
                    "size": 8,
                    "retention": "session",
                    "sensitivity": "private",
                    "suggested_name": "output.bin",
                }
            }
        return {"operation": operation}


class _Transfer:
    def __init__(self):
        self.calls = []

    async def execute_part(self, operation, payload):
        self.calls.append((operation, payload))
        return {"operation": operation}


class _Artifacts:
    def __init__(self):
        self.requests = []

    async def publish(self, request):
        self.requests.append(request)
        return ArtifactRevision(
            artifact_id="artifact-1",
            revision=1,
            representations=(
                ArtifactRef(
                    artifact_id="artifact-1",
                    revision=1,
                    representation="original",
                    kind="provider_file",
                    mime_type=request.representations[0].mime_type,
                    content_ref="sha256:" + "a" * 64,
                    digest="a" * 64,
                    size=len(request.representations[0].content),
                    retention=request.retention,
                    sensitivity=request.sensitivity,
                    suggested_name=request.representations[0].suggested_name,
                ),
            ),
        )


class _ModelGateway:
    def route_profiles(self, route):
        return ()


def test_compatibility_owners_map_routes_to_canonical_wire_operations():
    async def scenario():
        command, transfer = _Command(), _Transfer()
        durable = CommandCompatibilityOwner(command)
        artifacts = ArtifactTransferCompatibilityOwner(transfer)
        await durable.execute("batches", {"input_file_id": "f"})
        await durable.list("batches", {"limit": "10"})
        await durable.list("files", {})
        await artifacts.upload("files.upload", {"artifact_id": "a"})
        await durable.resource("batch.cancel", "batch-1")
        await durable.resource("file.content", "file-1")
        return command.calls, transfer.calls

    commands, transfers = asyncio.run(scenario())
    assert commands == [
        ("batch.create", {"input_file_id": "f"}),
        ("batch.list", {"limit": "10"}),
        ("file.list", {}),
        ("batch.cancel", {"batch_id": "batch-1"}),
        ("file.content", {"file_id": "file-1"}),
    ]
    assert transfers == [("file.upload", {"artifact_id": "a"})]


def test_multipart_upload_is_persisted_before_transfer():
    async def scenario():
        transfer, artifacts = _Transfer(), _Artifacts()
        owner = ArtifactTransferCompatibilityOwner(transfer, artifacts)
        result = await owner.upload_bytes(
            "files.upload",
            b"provider input",
            filename="input.jsonl",
            content_type="application/jsonl",
            fields={"purpose": "batch", "idempotency_key": "upload-1"},
        )
        return result, transfer.calls, artifacts.requests

    result, transfers, publications = asyncio.run(scenario())
    assert result == {"operation": "file.upload"}
    assert len(publications) == 1
    assert publications[0].idempotency_key == "upload-1"
    assert publications[0].representations[0].content == b"provider input"
    assert transfers[0][0] == "file.upload"
    assert transfers[0][1]["purpose"] == "batch"
    assert transfers[0][1]["artifact"]["content_ref"].startswith("sha256:")


def test_runtime_owners_project_through_public_http_routes():
    async def scenario():
        command, transfer = _Command(), _Transfer()
        app = build_inference_api(
            _ModelGateway(),
            bearer_token="secret",
            durable_operations=CommandCompatibilityOwner(command),
            artifact_operations=ArtifactTransferCompatibilityOwner(transfer),
        )
        headers = {"Authorization": "Bearer secret"}
        async with TestClient(TestServer(app)) as client:
            created = await client.post(
                "/v1/batches",
                headers=headers,
                json={"input_file_id": "file-1"},
            )
            batches = await client.get("/v1/batches?limit=2", headers=headers)
            uploaded = await client.post(
                "/v1/files",
                headers=headers,
                json={"artifact_id": "artifact-1"},
            )
            files = await client.get("/v1/files?purpose=batch", headers=headers)
            return (
                [created.status, batches.status, uploaded.status, files.status],
                [
                    await created.json(),
                    await batches.json(),
                    await uploaded.json(),
                    await files.json(),
                ],
                command.calls,
                transfer.calls,
            )

    statuses, responses, commands, transfers = asyncio.run(scenario())
    assert statuses == [200, 200, 200, 200]
    assert responses == [
        {"operation": "batch.create"},
        {"operation": "batch.list"},
        {"operation": "file.upload"},
        {"operation": "file.list"},
    ]
    assert commands == [
        ("batch.create", {"input_file_id": "file-1"}),
        ("batch.list", {"limit": "2"}),
        ("file.list", {"purpose": "batch"}),
    ]
    assert transfers == [("file.upload", {"artifact_id": "artifact-1"})]


def test_public_files_route_accepts_bounded_multipart_upload():
    async def scenario():
        transfer, artifacts = _Transfer(), _Artifacts()
        app = build_inference_api(
            _ModelGateway(),
            bearer_token="secret",
            artifact_operations=ArtifactTransferCompatibilityOwner(transfer, artifacts),
        )
        form = FormData()
        form.add_field("purpose", "batch")
        form.add_field(
            "file",
            b'{"custom_id":"one"}\n',
            filename="input.jsonl",
            content_type="application/jsonl",
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/files",
                headers={"Authorization": "Bearer secret"},
                data=form,
            )
            return response.status, await response.json(), transfer.calls, artifacts.requests

    status, response, transfers, publications = asyncio.run(scenario())
    assert status == 200, response
    assert response == {"operation": "file.upload"}
    assert publications[0].representations[0].suggested_name == "input.jsonl"
    assert transfers[0][1]["purpose"] == "batch"


def test_public_file_content_resolves_artifact_to_binary_response():
    async def scenario():
        command = _Command()

        async def read(ref):
            return ResolvedArtifact(ref=ref, content=b"download")

        app = build_inference_api(
            _ModelGateway(),
            bearer_token="secret",
            durable_operations=CommandCompatibilityOwner(command),
            artifact_reader=read,
        )
        async with TestClient(TestServer(app)) as client:
            response = await client.get(
                "/v1/files/file-1/content",
                headers={"Authorization": "Bearer secret"},
            )
            body = await response.read()
            return (
                response.status,
                body,
                dict(response.headers),
                command.calls,
            )

    status, content, headers, calls = asyncio.run(scenario())
    digest = hashlib.sha256(b"download").hexdigest()
    assert status == 200, content
    assert content == b"download"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Etag"] == f'"{digest}"'
    assert calls == [("file.content", {"file_id": "file-1"})]
