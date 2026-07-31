import asyncio

from mote.contracts.inference.governance import CredentialHealthObservation, ProviderQuotaObservation
from mote.runtime.inference.governance import CredentialHealthAuthority, ProviderQuotaAuthority


def test_provider_quota_cooldown_and_token_limits_are_independent():
    async def scenario():
        now = 10.0
        authority = ProviderQuotaAuthority(clock=lambda: now)
        assert await authority.allow("p", "e", "c", estimated_tokens=100)
        await authority.observe(
            ProviderQuotaObservation(
                provider="p",
                endpoint_id="e",
                credential_slot_id="c",
                kind="limits",
                remaining_requests=2,
                remaining_tokens=50,
            )
        )
        assert not await authority.allow("p", "e", "c", estimated_tokens=100)
        await authority.observe(
            ProviderQuotaObservation(
                provider="p",
                endpoint_id="e",
                credential_slot_id="c",
                kind="retry_after",
                retry_after_seconds=5,
            )
        )
        assert not await authority.allow("p", "e", "c", estimated_tokens=1)
        now = 16.0
        assert await authority.allow("p", "e", "c", estimated_tokens=1)

    asyncio.run(scenario())


def test_credential_health_refresh_quarantine_and_revoke_are_version_scoped():
    async def scenario():
        now = 0.0
        authority = CredentialHealthAuthority(clock=lambda: now)
        await authority.observe(
            CredentialHealthObservation(
                credential_slot_id="slot",
                credential_version="1",
                verdict="quarantine",
                quarantine_seconds=5,
                reason="authentication rejected",
            )
        )
        assert not await authority.allow("slot", "1")
        assert await authority.allow("slot", "2")
        now = 6.0
        assert await authority.allow("slot", "1")
        await authority.observe(
            CredentialHealthObservation(
                credential_slot_id="slot",
                credential_version="1",
                verdict="revoke",
                reason="operator revoked",
            )
        )
        assert not await authority.allow("slot", "1")

    asyncio.run(scenario())
