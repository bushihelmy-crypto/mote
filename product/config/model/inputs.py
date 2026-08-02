"""Product YAML syntax for shortcut and explicit model configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from mote.contracts.config.base import ConfigModel
from mote.contracts.model.operations import ModelOperation


def Sensitive(default: Any = None, **kwargs: Any) -> Any:
    extra = dict(kwargs.pop("json_schema_extra", {}) or {})
    extra["sensitive"] = True
    return Field(default=default, json_schema_extra=extra, **kwargs)


class ProductInput(ConfigModel):
    pass


class ProductOAuthInput(ProductInput):
    provider: str | None = None
    issuer: str | None = None
    token_url: str | None = None
    authorize_url: str | None = None
    device_authorization_url: str | None = None
    redirect_uri: str = "http://localhost:53692/callback"
    client_id: str | None = None
    client_secret: str | None = Sensitive(default=None)
    grant_type: str = "client_credentials"
    refresh_token: str | None = Sensitive(default=None)
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = None
    headers_extra: dict[str, str] = Field(default_factory=dict)
    expiry_buffer_s: int = Field(default=300, ge=0)


class ProductEndpointInput(ProductInput):
    provider: str | None = None
    api_key: str | list[str] | None = Sensitive(default=None)
    api_type: str | None = None
    base_url: str | None = Field(default=None, json_schema_extra={"untrusted_forbidden": True})
    oauth: ProductOAuthInput | None = Sensitive(default=None)
    model: str = Field(min_length=1)
    pricing_plan: str | None = None
    max_token: int = Field(default=4096, ge=1)
    temperature: float = 0.0
    timeout: int = Field(default=600, gt=0)
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    proxy: str | None = None
    calc_usage: bool = True
    use_prompt_cache: bool = True


class ProductEndpointCapabilitiesInput(ProductInput):
    supported_operations: frozenset[ModelOperation] | None = None
    supports_tools: bool | None = None
    supports_native_schema: bool | None = None
    supports_server_web_search: bool | None = None
    supports_vision: bool | None = None
    supports_pdf: bool | None = None
    supports_native_tool_search: bool | None = None
    context_tokens: int | None = Field(default=None, ge=0)


class ProductExplicitEndpointInput(ProductEndpointInput):
    credential_pool: str | None = None
    region: str = "global"
    governance_domain: str = "default"
    pricing_class: str = "default"
    capabilities: ProductEndpointCapabilitiesInput = Field(default_factory=ProductEndpointCapabilitiesInput)
    enabled: bool = True


class ProductCredentialSlotInput(ProductInput):
    id: str = Field(min_length=1)
    secret_ref: str = Field(min_length=1, json_schema_extra={"sensitive": True})


class ProductCredentialPoolInput(ProductInput):
    slots: list[ProductCredentialSlotInput] = Field(min_length=1)


class ProductFailoverGroupInput(ProductInput):
    endpoints: list[str] = Field(min_length=1)
    recovery_profile: str = Field(min_length=1)


class ProductRecoveryInput(ProductInput):
    max_wire_attempts: int = Field(default=6, ge=1)
    max_attempts_per_endpoint: int = Field(default=6, ge=1)
    max_endpoint_switches: int = Field(default=5, ge=0)
    max_credential_rotations: int = Field(default=5, ge=0)
    max_request_transforms: int = Field(default=5, ge=0)
    total_deadline_ms: int = Field(default=600_000, gt=0)
    single_attempt_timeout_ms: int = Field(default=180_000, gt=0)
    max_backoff_ms: int = Field(default=60_000, ge=0)


class ProductRoutesInput(ProductInput):
    default: str = Field(min_length=1)
    tasks: dict[str, str] = Field(default_factory=dict)
    semantic: dict[str, str] = Field(default_factory=dict)


class ApiKeyHelperInput(ProductInput):
    argv: tuple[str, ...] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def _fixed_executable(cls, argv: tuple[str, ...]) -> tuple[str, ...]:
        if any(not argument for argument in argv):
            raise ValueError("api_key_helper argv entries must be non-empty")
        if not Path(argv[0]).is_absolute():
            raise ValueError("api_key_helper executable must be an absolute path")
        return argv


class ShortcutModelsConfig(ProductInput):
    mode: Literal["shortcut"] = "shortcut"
    default: ProductEndpointInput
    tasks: dict[str, ProductEndpointInput] = Field(default_factory=dict)
    recovery_defaults: ProductRecoveryInput = Field(default_factory=ProductRecoveryInput)
    api_key_helper: ApiKeyHelperInput | None = Sensitive(default=None)
    response_language: str = "chinese"


class ExplicitModelsConfig(ProductInput):
    mode: Literal["explicit"]
    endpoints: dict[str, ProductExplicitEndpointInput]
    credential_pools: dict[str, ProductCredentialPoolInput] = Field(
        default_factory=dict,
        json_schema_extra={"sensitive": True},
    )
    failover_groups: dict[str, ProductFailoverGroupInput]
    routes: ProductRoutesInput
    recovery_profiles: dict[str, ProductRecoveryInput]
    response_language: str = "chinese"

    @model_validator(mode="after")
    def _graph_is_closed(self) -> "ExplicitModelsConfig":
        if not self.endpoints or not self.failover_groups or not self.recovery_profiles:
            raise ValueError("explicit model topology cannot contain empty endpoint/group/profile maps")
        for group_id, group in self.failover_groups.items():
            if any(endpoint not in self.endpoints for endpoint in group.endpoints):
                raise ValueError(f"group {group_id!r} references an unknown endpoint")
            if group.recovery_profile not in self.recovery_profiles:
                raise ValueError(f"group {group_id!r} references an unknown recovery profile")
        groups = set(self.failover_groups)
        route_groups = {
            self.routes.default,
            *self.routes.tasks.values(),
            *self.routes.semantic.values(),
        }
        if not route_groups <= groups:
            raise ValueError("model route references an unknown failover group")
        overlap = set(self.routes.tasks) & set(self.routes.semantic)
        if overlap:
            raise ValueError(f"task and semantic route names must be disjoint: {sorted(overlap)!r}")
        return self

    @field_validator("endpoints", "failover_groups", "recovery_profiles")
    @classmethod
    def _non_empty_ids(cls, values: dict[str, Any]) -> dict[str, Any]:
        if any(not value for value in values):
            raise ValueError("model topology ids cannot be empty")
        return values


ProductModelsConfig = Annotated[
    ShortcutModelsConfig | ExplicitModelsConfig,
    Field(discriminator="mode"),
]
_PRODUCT_MODELS_ADAPTER = TypeAdapter(ProductModelsConfig)


def parse_product_models_config(value: Any) -> ProductModelsConfig:
    return _PRODUCT_MODELS_ADAPTER.validate_python(value)


__all__ = [
    "ExplicitModelsConfig",
    "ApiKeyHelperInput",
    "ProductCredentialPoolInput",
    "ProductEndpointInput",
    "ProductExplicitEndpointInput",
    "ProductModelsConfig",
    "ProductRecoveryInput",
    "ShortcutModelsConfig",
    "parse_product_models_config",
]
