import asyncio
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.security.wire_permit import (
    Ed25519WirePermitSigner,
    Ed25519WirePermitVerifier,
    canonical_wire_permit_payload,
)


def _permit():
    now = datetime.now(timezone.utc)
    return WirePermit(
        attempt_id="attempt",
        execution_taxonomy="unary_finite_attempt",
        owner_journal_id="journal",
        wire_unit="generate",
        generation_id="generation",
        generation_artifact_digest="sha256:" + "e" * 64,
        ordinal=1,
        nonce="0123456789abcdef",
        issued_journal_revision=1,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="issuer-1",
        audience="embedded",
        trust_revision=3,
        backup_epoch=0,
        admission_epoch=0,
        signature="unsigned",
    )


def test_ed25519_wire_permit_is_bound_and_revocable():
    async def scenario():
        private_key = Ed25519PrivateKey.generate()
        signer = Ed25519WirePermitSigner(
            issuer_key_id="issuer-1",
            trust_revision=3,
            private_key=private_key,
        )
        signed = signer.sign(_permit())
        verifier = Ed25519WirePermitVerifier({("issuer-1", 3): private_key.public_key()})
        assert await verifier.verify(signed)
        assert not await verifier.verify(signed.model_copy(update={"wire_unit": "different"}))
        revoked = Ed25519WirePermitVerifier(
            {("issuer-1", 3): private_key.public_key()},
            revoked_keys=frozenset({("issuer-1", 3)}),
        )
        assert not await revoked.verify(signed)
        assert b'"signature"' not in canonical_wire_permit_payload(signed)

    asyncio.run(scenario())
