import asyncio

from aiohttp.test_utils import TestClient, TestServer

from mote.product.interfaces.inference_admin_api import AdminMutationModel, AdminReadModel, build_inference_admin_api


def _read_model():
    async def providers():
        return ({"provider": "openai", "enabled": True},)

    async def empty():
        return ()

    async def readiness():
        return {"ready": True, "components": {"admission": "ready"}}

    async def receipt(execution_id):
        return {
            "execution_id": execution_id,
            "provider_request_present": True,
            "terminal_artifact_present": False,
        }

    async def audit(after):
        return ({"sequence": after + 1, "operation": "backup", "outcome": "committed"},)

    return AdminReadModel(
        providers=providers,
        credentials=empty,
        generations=empty,
        readiness=readiness,
        receipt=receipt,
        reconciliation=empty,
        audit=audit,
    )


def test_admin_api_is_authenticated_scoped_and_redacted():
    async def scenario():
        app = build_inference_admin_api(_read_model(), bearer_token="admin-secret")
        async with TestClient(TestServer(app)) as client:
            denied = await client.get("/admin/v1/providers")
            assert denied.status == 401
            headers = {"Authorization": "Bearer admin-secret"}
            providers = await client.get("/admin/v1/providers", headers=headers)
            assert (await providers.json())["items"] == [{"provider": "openai", "enabled": True}]
            receipt = await client.get("/admin/v1/receipts/execution", headers=headers)
            document = await receipt.json()
            assert document["receipt"]["execution_id"] == "execution"
            assert "provider_request_id" not in document["receipt"]
            audit = await client.get("/admin/v1/audit?after=4", headers=headers)
            assert (await audit.json())["items"][0]["sequence"] == 5

    asyncio.run(scenario())


def test_admin_api_delegated_authorizer_receives_exact_scope():
    class Authorizer:
        def __init__(self):
            self.scopes = []

        async def authorize(self, bearer_token, scope):
            self.scopes.append(scope)
            return scope == "operations.read"

    async def scenario():
        authorizer = Authorizer()
        app = build_inference_admin_api(_read_model(), authorizer=authorizer)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/admin/v1/readiness")).status == 200
            assert (await client.get("/admin/v1/providers")).status == 401
        return authorizer.scopes

    assert asyncio.run(scenario()) == ["operations.read", "providers.read"]


def test_generation_stage_delegates_to_generation_owner_adapter():
    async def scenario():
        calls = []

        async def stage(artifact, generation_id, digest):
            calls.append((artifact, generation_id, digest))
            return {
                "generation_id": generation_id,
                "artifact_digest": digest,
                "state": "staged",
            }

        app = build_inference_admin_api(
            _read_model(),
            bearer_token="admin-secret",
            mutations=AdminMutationModel(stage_generation=stage),
        )
        digest = "sha256:" + "a" * 64
        artifact = {
            "generation_id": "generation-one",
            "artifact_digest": digest,
        }
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/admin/v1/generations/stage",
                headers={"Authorization": "Bearer admin-secret"},
                json={
                    "generation_id": "generation-one",
                    "artifact_digest": digest,
                    "generation_artifact": artifact,
                },
            )
            assert response.status == 202
            assert (await response.json())["generation"]["state"] == "staged"
        return calls

    calls = asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][1:] == ("generation-one", "sha256:" + "a" * 64)


def test_generation_activation_requires_exact_scope_and_digest_binding():
    class Authorizer:
        def __init__(self):
            self.scopes = []

        async def authorize(self, bearer_token, scope):
            self.scopes.append(scope)
            return scope == "generations.activate"

    async def scenario():
        calls = []
        authorizer = Authorizer()

        async def stage(artifact, generation_id, digest):
            raise AssertionError("stage should not be called")

        async def activate(generation_id, digest):
            calls.append((generation_id, digest))
            return {
                "generation_id": generation_id,
                "artifact_digest": digest,
                "state": "active",
            }

        app = build_inference_admin_api(
            _read_model(),
            authorizer=authorizer,
            mutations=AdminMutationModel(stage_generation=stage, activate_generation=activate),
        )
        digest = "sha256:" + "b" * 64
        async with TestClient(TestServer(app)) as client:
            invalid = await client.post(
                "/admin/v1/generations/activate",
                json={"generation_id": "g", "artifact_digest": "sha256:short"},
            )
            assert invalid.status == 400
            activated = await client.post(
                "/admin/v1/generations/activate",
                json={"generation_id": "g", "artifact_digest": digest},
            )
            assert activated.status == 202
            assert (await activated.json())["generation"]["state"] == "active"
        return authorizer.scopes, calls

    scopes, calls = asyncio.run(scenario())
    assert scopes == ["generations.activate", "generations.activate"]
    assert calls == [("g", "sha256:" + "b" * 64)]
