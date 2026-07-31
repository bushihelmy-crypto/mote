import asyncio

from mote.product.models.secrets import (
    CredentialEpoch,
    CredentialMaterial,
    CredentialWireAccess,
    InMemorySecretHandle,
    SecretIdentity,
)


def test_endpoint_wire_access_is_explicit_and_material_is_short_lived() -> None:
    async def scenario() -> None:
        handle = InMemorySecretHandle(
            endpoint_id="endpoint",
            slot_id="slot",
            identity=SecretIdentity("identity"),
            epoch=CredentialEpoch("epoch"),
            value="canary-secret",
        )
        lease = await handle.acquire()
        material: CredentialMaterial = await lease.resolve()
        assert material.read_for_wire(CredentialWireAccess("endpoint", "slot")) == "canary-secret"
        material.release()
        await lease.release()
        await handle.aclose()

    asyncio.run(scenario())
