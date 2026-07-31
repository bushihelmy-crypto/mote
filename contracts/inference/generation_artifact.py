from typing import Any, Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract


class GenerationArtifact(FrozenContract):
    schema_version: Literal[1] = 1
    generation_id: str = Field(min_length=1)
    parent_generation_id: str | None = None
    model_planner_and_bindings: dict[str, Any]
    service_planner_and_bindings: dict[str, Any]
    session_capability_and_bindings: dict[str, Any]
    transfer_capability_and_bindings: dict[str, Any]
    credential_versions: dict[str, str]
    transport_registry_revision: str = Field(min_length=1)
    client_profile_revision: str = Field(min_length=1)
    failure_policy_revision: str = Field(min_length=1)
    capability_catalog_pricing_snapshot: dict[str, Any]
    governance_cache_plugin_revisions: dict[str, str]
    required_wire_contract_range: tuple[int, int]
    activation_policy: dict[str, Any]
    min_reader_version: int = Field(ge=1)
    min_writer_version: int = Field(ge=1)
    persistence_schema_versions: dict[str, int]
    migration_set_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signer_key_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)
