"""Product-owned construction and signing of model wire permits."""

from __future__ import annotations

import secrets
from datetime import datetime

from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit
from mote.product.inference.security.wire_permit import Ed25519WirePermitSigner


class ProductWirePermitIssuer:
    def __init__(
        self,
        signer: Ed25519WirePermitSigner,
        *,
        issuer_key_id: str,
        trust_revision: int,
    ) -> None:
        if not issuer_key_id or trust_revision <= 0:
            raise ValueError("permit issuer identity is invalid")
        self._signer = signer
        self._issuer_key_id = issuer_key_id
        self._trust_revision = trust_revision

    def issue(
        self,
        *,
        attempt_id: str,
        execution_taxonomy: ExecutionTaxonomy,
        owner_journal_id: str,
        wire_unit: str,
        generation_id: str,
        generation_artifact_digest: str,
        ordinal: int,
        issued_journal_revision: int,
        not_before: datetime,
        expires_at: datetime,
        audience: str,
        backup_epoch: int,
        admission_epoch: int,
    ) -> WirePermit:
        unsigned = WirePermit(
            attempt_id=attempt_id,
            execution_taxonomy=execution_taxonomy,
            owner_journal_id=owner_journal_id,
            wire_unit=wire_unit,
            generation_id=generation_id,
            generation_artifact_digest=generation_artifact_digest,
            ordinal=ordinal,
            nonce=secrets.token_urlsafe(24),
            issued_journal_revision=issued_journal_revision,
            not_before=not_before,
            expires_at=expires_at,
            issuer_key_id=self._issuer_key_id,
            audience=audience,
            trust_revision=self._trust_revision,
            backup_epoch=backup_epoch,
            admission_epoch=admission_epoch,
            signature="unsigned",
        )
        return self._signer.sign(unsigned)


__all__ = ["ProductWirePermitIssuer"]
