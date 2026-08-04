from __future__ import annotations

import asyncio
import time

import pytest

from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.product.models.credential_sources import OAuthSecretHandle
from mote.product.models.secrets import CredentialEpoch, CredentialWireAccess, SecretIdentity
from mote.runtime.models.auth.oauth.manager import OAuthManager
from mote.runtime.models.auth.oauth.models import OAuthToken


class _Client:
    def client_credentials(self) -> OAuthToken:
        return OAuthToken(access_token="consumer-bound", expires_at=time.time() + 3600)


def test_product_oauth_consumer_holds_and_releases_generation_borrow(tmp_path) -> None:
    config = OAuthProviderConfig(
        token_url="https://issuer/token",
        storage_root=tmp_path,
    )
    manager = OAuthManager(
        config,
        provider="provider",
        consumer_id="model-endpoint:endpoint:slot:slot",
        client=_Client(),
    )
    handle = OAuthSecretHandle(
        endpoint_id="endpoint",
        slot_id="slot",
        identity=SecretIdentity("identity"),
        epoch=CredentialEpoch("epoch"),
        manager=manager,
        force_refresh=False,
    )

    lease = asyncio.run(handle.acquire())
    material = asyncio.run(lease.resolve())
    assert material.read_for_wire(CredentialWireAccess("endpoint", "slot")) == "consumer-bound"
    markers = list((tmp_path / "borrows" / str(manager._store.subject)).glob("*.json"))
    assert len(markers) == 1

    material.release()
    asyncio.run(lease.release())
    assert not markers[0].exists()
    with pytest.raises(RuntimeError, match="released"):
        asyncio.run(lease.resolve())
