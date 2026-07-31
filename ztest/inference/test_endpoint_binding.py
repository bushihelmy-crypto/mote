from mote.contracts.model.failover import EndpointDescriptor
from mote.product.models.bindings import ProductModelBindingResolver
from mote.product.models.compiler import CredentialBindingSpec
from mote.product.models.secrets import CredentialEpoch, InMemorySecretHandle, SecretIdentity


def test_endpoint_binding_resolution_is_side_effect_free_and_version_pinned():
    handle = InMemorySecretHandle(
        endpoint_id="endpoint",
        slot_id="slot",
        identity=SecretIdentity("secret-identity"),
        epoch=CredentialEpoch("epoch-7"),
        value="must-not-appear",
    )
    resolver = ProductModelBindingResolver(CredentialBindingSpec(handles={"slot": handle}))
    endpoint = EndpointDescriptor(
        endpoint_id="endpoint",
        transport="openai_chat",
        provider="openai",
        model="gpt",
        base_url_identity="https://api.openai.com",
        credential_pool_id="pool",
        lifecycle_revision="1",
    )
    first = resolver.resolve(endpoint, "slot")
    second = resolver.resolve(endpoint, "slot")
    assert first == second
    assert first is not None
    assert first.credential_version == "epoch-7"
    assert first.transport_identity.startswith("sha256:")
    assert "must-not-appear" not in first.model_dump_json()
    assert resolver.resolve(endpoint, "missing") is None
    assert endpoint.execution_policy.max_output_tokens == 4096
