"""Declarative endpoint, failover-group, route, and recovery configuration."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from mote.contracts.config.base import ConfigModel
from mote.contracts.config.model.llm import LLMConfig
from mote.contracts.model.failover import AttemptBudget


class CredentialSlotConfig(ConfigModel):
    id: str = Field(min_length=1)
    secret_ref: str = Field(min_length=1, pattern=r"^env://[A-Za-z_][A-Za-z0-9_]*$")


class CredentialPoolConfig(ConfigModel):
    slots: list[CredentialSlotConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _slot_ids_are_unique(self) -> "CredentialPoolConfig":
        ids = [slot.id for slot in self.slots]
        if len(ids) != len(set(ids)):
            raise ValueError("credential pool contains duplicate slot ids")
        return self


class EndpointCapabilitiesConfig(ConfigModel):
    supports_tools: bool | None = None
    supports_native_schema: bool | None = None
    supports_server_web_search: bool | None = None
    supports_vision: bool | None = None
    supports_pdf: bool | None = None
    supports_native_tool_search: bool | None = None
    context_tokens: int | None = Field(default=None, ge=0)


class ModelEndpointConfig(LLMConfig):
    """One named provider endpoint plus non-wire governance metadata."""

    credential_pool: str | None = None
    region: str = "global"
    governance_domain: str = "default"
    pricing_class: str = "default"
    capabilities: EndpointCapabilitiesConfig = Field(default_factory=EndpointCapabilitiesConfig)
    enabled: bool = True

    @model_validator(mode="after")
    def _model_is_explicit(self) -> "ModelEndpointConfig":
        if not self.model:
            raise ValueError("model endpoint requires a model")
        return self


class RecoveryProfileConfig(ConfigModel):
    max_wire_attempts: int = Field(default=6, ge=1)
    max_attempts_per_endpoint: int = Field(default=6, ge=1)
    max_endpoint_switches: int = Field(default=5, ge=0)
    max_credential_rotations: int = Field(default=5, ge=0)
    max_request_transforms: int = Field(default=5, ge=0)
    total_deadline_seconds: float = Field(default=600.0, gt=0.0)
    single_attempt_timeout_seconds: float = Field(default=180.0, gt=0.0)
    max_backoff_seconds: float = Field(default=60.0, ge=0.0)

    @model_validator(mode="after")
    def _validate_budget(self) -> "RecoveryProfileConfig":
        self.to_attempt_budget()
        return self

    def to_attempt_budget(self) -> AttemptBudget:
        return AttemptBudget(**self.model_dump())


class FailoverGroupConfig(ConfigModel):
    endpoints: list[str] = Field(min_length=1)
    recovery_profile: str = "default"

    @field_validator("endpoints")
    @classmethod
    def _endpoints_are_unique(cls, endpoints: list[str]) -> list[str]:
        if any(not endpoint for endpoint in endpoints):
            raise ValueError("failover endpoint id cannot be empty")
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("failover group contains duplicate endpoints")
        return endpoints


class ModelRoutesConfig(ConfigModel):
    default: str | None = None
    tasks: dict[str, str] = Field(default_factory=dict)
    semantic: dict[str, str] = Field(default_factory=dict)


def default_recovery_profiles() -> dict[str, RecoveryProfileConfig]:
    return {"default": RecoveryProfileConfig()}


def ensure_default_recovery_profile(values: Any) -> Any:
    """Inject the one canonical default without overwriting user profiles."""

    if not isinstance(values, dict):
        return values
    profiles = values.get("recovery_profiles")
    if profiles is None:
        return values
    if isinstance(profiles, dict) and "default" not in profiles:
        values = dict(values)
        values["recovery_profiles"] = {"default": {}, **profiles}
    return values


__all__ = [
    "CredentialPoolConfig",
    "CredentialSlotConfig",
    "EndpointCapabilitiesConfig",
    "FailoverGroupConfig",
    "ModelEndpointConfig",
    "ModelRoutesConfig",
    "RecoveryProfileConfig",
    "default_recovery_profiles",
    "ensure_default_recovery_profile",
]
