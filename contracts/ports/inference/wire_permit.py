from datetime import datetime
from typing import Protocol

from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit


class WirePermitIssuer(Protocol):
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
        ...


class WirePermitVerifier(Protocol):
    async def verify(self, permit: WirePermit) -> bool:
        ...
