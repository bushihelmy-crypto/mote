"""Side-effect-free configuration for Embedded and Shared Process inference."""

from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_network

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeploymentMode(StrEnum):
    EMBEDDED = "embedded"
    SHARED_PROCESS = "shared_process"


class AdmissionPolicy(StrEnum):
    REJECT = "reject"
    WAIT = "wait"
    DEADLINE = "deadline"


class PersistenceBackend(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class PluginIsolation(StrEnum):
    TRUSTED_PYTHON = "trusted_python"
    WASM = "wasm"
    SUBPROCESS = "subprocess"


class CapacityConfig(_Config):
    global_in_flight: int = Field(default=1000, ge=1)
    provider_in_flight: int = Field(default=100, ge=1)
    endpoint_in_flight: int = Field(default=100, ge=1)
    queue_capacity: int = Field(default=5000, ge=1)
    event_buffer_capacity: int = Field(default=256, ge=1)
    admission_policy: AdmissionPolicy = AdmissionPolicy.DEADLINE

    @model_validator(mode="after")
    def _nested_capacities_fit_global_limit(self) -> "CapacityConfig":
        if self.endpoint_in_flight > self.provider_in_flight:
            raise ValueError("endpoint_in_flight cannot exceed provider_in_flight")
        if self.provider_in_flight > self.global_in_flight:
            raise ValueError("provider_in_flight cannot exceed global_in_flight")
        return self


class DeadlineConfig(_Config):
    default_seconds: float = Field(default=300.0, gt=0)
    clock_skew_guard_seconds: float = Field(default=2.0, ge=0)
    maximum_seconds: float = Field(default=3600.0, gt=0)

    @model_validator(mode="after")
    def _default_fits_maximum(self) -> "DeadlineConfig":
        if self.default_seconds > self.maximum_seconds:
            raise ValueError("default deadline cannot exceed maximum")
        return self


class PrivateNetworkPolicy(_Config):
    allow_private_network: bool = False
    allowed_cidrs: tuple[str, ...] = ()
    allowed_dns_suffixes: tuple[str, ...] = ()
    allow_redirects: bool = False
    revalidate_dns_on_connect: bool = True
    revalidate_redirect_target: bool = True
    block_link_local: bool = True
    block_metadata_endpoints: bool = True

    @field_validator("allowed_cidrs")
    @classmethod
    def _cidrs_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(str(ip_network(value, strict=True)) for value in values)
        if len(canonical) != len(set(canonical)):
            raise ValueError("allowed_cidrs contains duplicates")
        return canonical

    @field_validator("allowed_dns_suffixes")
    @classmethod
    def _dns_suffixes_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(value.lower().rstrip(".") for value in values)
        if any(not value or value.startswith(".") for value in canonical):
            raise ValueError("DNS suffixes must be non-empty canonical suffixes")
        if len(canonical) != len(set(canonical)):
            raise ValueError("allowed_dns_suffixes contains duplicates")
        return canonical

    @model_validator(mode="after")
    def _allowlists_require_private_network_opt_in(self) -> "PrivateNetworkPolicy":
        if not self.allow_private_network and (self.allowed_cidrs or self.allowed_dns_suffixes):
            raise ValueError("private allowlists require allow_private_network=true")
        if not self.block_link_local or not self.block_metadata_endpoints:
            raise ValueError("link-local and metadata endpoints are always blocked")
        return self


class PersistenceConfig(_Config):
    backend: PersistenceBackend = PersistenceBackend.SQLITE
    dsn_secret_ref: str | None = Field(default=None, pattern=r"^env://[A-Za-z_][A-Za-z0-9_]*$")
    receipt_retention_days: int = Field(default=90, ge=1)
    outbox_retention_days: int = Field(default=30, ge=1)
    encryption_key_ref: str = Field(pattern=r"^env://[A-Za-z_][A-Za-z0-9_]*$")
    shared_sqlite: "SharedSQLiteConfig" = Field(default_factory=lambda: SharedSQLiteConfig())

    @model_validator(mode="after")
    def _backend_has_required_binding(self) -> "PersistenceConfig":
        if self.backend is PersistenceBackend.POSTGRESQL and self.dsn_secret_ref is None:
            raise ValueError("PostgreSQL persistence requires dsn_secret_ref")
        return self


class SharedSQLiteConfig(_Config):
    wal_enabled: bool = True
    synchronous: str = Field(default="FULL", pattern=r"^FULL$")
    foreign_keys: bool = True
    busy_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    checkpoint_bytes: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024)
    checkpoint_seconds: int = Field(default=300, ge=1)
    soft_disk_free_bytes: int = Field(default=2 * 1024**3, ge=1)
    hard_disk_free_bytes: int = Field(default=512 * 1024**2, ge=1)
    backup_interval_seconds: int = Field(default=3600, ge=60)
    restore_drill_interval_days: int = Field(default=30, ge=1)
    quick_check_on_start: bool = True

    @model_validator(mode="after")
    def _durability_and_watermarks_are_safe(self) -> "SharedSQLiteConfig":
        if not self.wal_enabled or not self.foreign_keys:
            raise ValueError("Shared SQLite requires WAL and foreign keys")
        if self.hard_disk_free_bytes >= self.soft_disk_free_bytes:
            raise ValueError("hard disk watermark must be below soft watermark")
        return self


class SharedProcessConfig(_Config):
    runtime_directory: str = Field(default="runtime/inference/shared", min_length=1)
    peer_credentials_required: bool = True
    session_credential_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    rpc_contract_versions: tuple[int, ...] = (3, 2)

    @field_validator("runtime_directory")
    @classmethod
    def _socket_is_relative(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("shared runtime_directory must be relative and traversal-free")
        return value


class PluginConfig(_Config):
    name: str = Field(min_length=1)
    isolation: PluginIsolation
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    enabled: bool = True


class CompatibilityConfig(_Config):
    inference_api_enabled: bool = True
    admin_api_enabled: bool = True
    realtime_enabled: bool = True
    webhook_enabled: bool = True
    max_body_bytes: int = Field(default=32 * 1024 * 1024, ge=1)
    max_stream_frame_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    max_precommit_bytes: int = Field(default=1024 * 1024, ge=1)
    max_precommit_frames: int = Field(default=1024, ge=1)
    max_precommit_seconds: float = Field(default=15.0, gt=0)


class ExactCacheConfig(_Config):
    enabled: bool = False
    default_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    maximum_entries: int = Field(default=1000, ge=1)
    sensitive_data_allowed: bool = False


class SemanticCacheConfig(_Config):
    enabled: bool = False
    backend: str | None = Field(default=None, pattern=r"^(redis|qdrant)$")
    threshold: float = Field(default=0.95, ge=0, le=1)

    @model_validator(mode="after")
    def _enabled_backend_is_explicit(self) -> "SemanticCacheConfig":
        if self.enabled and self.backend is None:
            raise ValueError("semantic cache requires an explicit backend")
        return self


class InferenceCacheConfig(_Config):
    exact: ExactCacheConfig = Field(default_factory=ExactCacheConfig)
    semantic: SemanticCacheConfig = Field(default_factory=SemanticCacheConfig)
    provider_prompt_cache_enabled: bool = True
    http_management_cache_enabled: bool = True


class InferenceConfig(_Config):
    schema_version: int = Field(default=1, ge=1)
    deployment: DeploymentMode = DeploymentMode.EMBEDDED
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)
    deadline: DeadlineConfig = Field(default_factory=DeadlineConfig)
    network: PrivateNetworkPolicy = Field(default_factory=PrivateNetworkPolicy)
    persistence: PersistenceConfig
    shared_process: SharedProcessConfig | None = None
    plugins: tuple[PluginConfig, ...] = ()
    compatibility: CompatibilityConfig = Field(default_factory=CompatibilityConfig)
    cache: InferenceCacheConfig = Field(default_factory=InferenceCacheConfig)

    @model_validator(mode="after")
    def _deployment_has_exact_configuration(self) -> "InferenceConfig":
        if self.deployment is DeploymentMode.SHARED_PROCESS and self.shared_process is None:
            raise ValueError("shared_process deployment requires shared_process config")
        if self.deployment is DeploymentMode.EMBEDDED and self.shared_process is not None:
            raise ValueError("embedded deployment cannot carry shared_process config")
        names = [plugin.name for plugin in self.plugins]
        if len(names) != len(set(names)):
            raise ValueError("plugin names must be unique")
        return self
