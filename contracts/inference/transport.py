from typing import Any, Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract
from mote.contracts.inference.governance import CredentialHealthObservation, ProviderQuotaObservation
from mote.contracts.model.failover import FailureDisposition


class ProviderWireResult(FrozenContract):
    schema_version: Literal[1] = 1
    payload: dict[str, Any]
    usage_units: int | None = Field(default=None, ge=0)
    quota_observation: ProviderQuotaObservation | None = None
    credential_observation: CredentialHealthObservation | None = None


class ProviderTransportFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        disposition: FailureDisposition,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.disposition = disposition
        self.retry_after_seconds = retry_after_seconds
