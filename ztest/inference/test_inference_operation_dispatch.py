import asyncio

from aiohttp.test_utils import TestClient, TestServer

from mote.product.interfaces.inference_api import build_inference_api


class _Gateway:
    def route_profiles(self, route):
        return ()


class _Unary:
    def __init__(self):
        self.calls = []

    async def execute(self, operation, payload):
        self.calls.append((operation, payload))
        return {"object": operation}


class _Durable:
    def __init__(self):
        self.calls = []

    async def execute(self, operation, payload):
        self.calls.append(("execute", operation, payload))
        return {"object": operation, "status": "accepted"}

    async def list(self, operation, query):
        self.calls.append(("list", operation, query))
        return {"object": "list", "data": []}

    async def resource(self, operation, resource_id):
        self.calls.append(("resource", operation, resource_id))
        return {"object": operation, "id": resource_id}

    async def content(self, resource_id):
        raise RuntimeError("not configured in this dispatch test")


class _Artifacts:
    def __init__(self):
        self.calls = []

    async def upload(self, operation, payload):
        self.calls.append((operation, payload))
        return {"object": "file", "status": "uploaded"}


def test_compatibility_operations_dispatch_to_exact_execution_owner():
    async def scenario():
        unary, durable, artifacts = _Unary(), _Durable(), _Artifacts()
        app = build_inference_api(
            _Gateway(),
            bearer_token="secret",
            unary_operations=unary,
            durable_operations=durable,
            artifact_operations=artifacts,
        )
        headers = {"Authorization": "Bearer secret"}
        async with TestClient(TestServer(app)) as client:
            for path in (
                "/v1/embeddings",
                "/v1/images/generations",
                "/v1/audio/speech",
                "/v1/audio/transcriptions",
            ):
                response = await client.post(path, headers=headers, json={"model": "m"})
                assert response.status == 200
            assert (await client.post("/v1/files", headers=headers, json={"artifact_id": "a"})).status == 200
            assert (await client.get("/v1/files?limit=10", headers=headers)).status == 200
            assert (await client.post("/v1/batches", headers=headers, json={"input_file_id": "f"})).status == 200
            assert (await client.get("/v1/batches", headers=headers)).status == 200
            assert (await client.get("/v1/batches/batch-1", headers=headers)).status == 200
            assert (await client.post("/v1/batches/batch-1/cancel", headers=headers)).status == 200
            assert (await client.delete("/v1/files/file-1", headers=headers)).status == 200
            assert (await client.post("/v1/videos", headers=headers, json={"prompt": "ocean"})).status == 200
            assert (await client.get("/v1/videos", headers=headers)).status == 200
            assert (await client.get("/v1/videos/video-1", headers=headers)).status == 200
            assert (
                await client.post("/v1/videos/video-1/remix", headers=headers, json={"prompt": "sunset"})
            ).status == 200
            assert (await client.post("/v1/containers", headers=headers, json={"name": "box"})).status == 200
            assert (await client.get("/v1/containers?limit=2", headers=headers)).status == 200
            assert (await client.delete("/v1/containers/container-1", headers=headers)).status == 200
        return unary, durable, artifacts

    unary, durable, artifacts = asyncio.run(scenario())
    assert [operation for operation, _payload in unary.calls] == [
        "embeddings.create",
        "images.generate",
        "audio.speech",
        "audio.transcriptions",
    ]
    assert artifacts.calls == [("files.upload", {"artifact_id": "a"})]
    assert durable.calls == [
        ("list", "files", {"limit": "10"}),
        ("execute", "batches", {"input_file_id": "f"}),
        ("list", "batches", {}),
        ("resource", "batch.retrieve", "batch-1"),
        ("resource", "batch.cancel", "batch-1"),
        ("resource", "file.delete", "file-1"),
        ("execute", "videos", {"prompt": "ocean"}),
        ("list", "videos", {}),
        ("resource", "video.retrieve", "video-1"),
        ("resource", "video.remix", "video-1"),
        ("execute", "containers", {"name": "box"}),
        ("list", "containers", {"limit": "2"}),
        ("resource", "container.delete", "container-1"),
    ]


def test_unconfigured_operation_owners_fail_closed():
    async def scenario():
        app = build_inference_api(_Gateway(), bearer_token="secret")
        headers = {"Authorization": "Bearer secret"}
        async with TestClient(TestServer(app)) as client:
            statuses = []
            for method, path in (
                ("post", "/v1/files"),
                ("get", "/v1/batches"),
            ):
                response = await getattr(client, method)(path, headers=headers, json={} if method == "post" else None)
                statuses.append(response.status)
            return statuses

    assert asyncio.run(scenario()) == [503, 503]


def test_stream_flag_is_rejected_before_unary_owner_execution():
    async def scenario():
        unary = _Unary()
        app = build_inference_api(_Gateway(), bearer_token="secret", unary_operations=unary)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer secret"},
                json={"stream": True},
            )
            return response.status, unary.calls

    assert asyncio.run(scenario()) == (400, [])
