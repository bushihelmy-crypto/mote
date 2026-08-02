"""Durable, protocol-explicit Toolset identity contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace

from mote.contracts.events.envelope import freeze_json, thaw_json
from mote.contracts.tool.protocol import CommandProtocol

TOOL_INVOCATION_IDENTITY_SCHEMA = "mote.tool-invocation-identity/v1"


@dataclass(frozen=True, slots=True)
class ToolInvocationId:
    """Nominal logical identity minted once by the Tool execution owner."""

    value: str

    def __post_init__(self) -> None:
        value = self.value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("ToolInvocationId must be a non-empty string")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ToolAttemptOrdinal:
    value: int

    def __post_init__(self) -> None:
        value = self.value
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("ToolAttemptOrdinal must be a positive integer")

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True)
class ToolInvocationIdentity:
    """Identity facts shared by policy, effect execution, settlement, and views."""

    invocation_id: ToolInvocationId
    attempt_ordinal: ToolAttemptOrdinal
    definition_identity: str
    catalog_generation: int
    arguments_digest: str
    owner_id: str
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.invocation_id, ToolInvocationId):
            raise TypeError("ToolInvocationIdentity.invocation_id must be ToolInvocationId")
        if not isinstance(self.attempt_ordinal, ToolAttemptOrdinal):
            raise TypeError("ToolInvocationIdentity.attempt_ordinal must be ToolAttemptOrdinal")
        for field_name in ("definition_identity", "arguments_digest", "owner_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ToolInvocationIdentity.{field_name} must not be empty")
        if isinstance(self.catalog_generation, bool) or self.catalog_generation < 1:
            raise ValueError("ToolInvocationIdentity.catalog_generation must be positive")
        if not isinstance(self.run_id, str):
            raise TypeError("ToolInvocationIdentity.run_id must be a string")

    def with_arguments(self, arguments: Mapping[str, object]) -> "ToolInvocationIdentity":
        return dataclass_replace(self, arguments_digest=tool_arguments_digest(arguments))

    def to_payload(self) -> dict[str, object]:
        """Project the exact versioned durable identity envelope."""
        return {
            "schema": TOOL_INVOCATION_IDENTITY_SCHEMA,
            "invocation_id": self.invocation_id.value,
            "attempt_ordinal": self.attempt_ordinal.value,
            "definition_identity": self.definition_identity,
            "catalog_generation": self.catalog_generation,
            "arguments_digest": self.arguments_digest,
            "owner_id": self.owner_id,
            "run_id": self.run_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ToolInvocationIdentity":
        """Decode a durable identity strictly; unknown shapes fail closed."""
        fields = {
            "schema",
            "invocation_id",
            "attempt_ordinal",
            "definition_identity",
            "catalog_generation",
            "arguments_digest",
            "owner_id",
            "run_id",
        }
        if set(payload) != fields:
            raise ValueError("tool invocation identity has unsupported fields")
        if payload["schema"] != TOOL_INVOCATION_IDENTITY_SCHEMA:
            raise ValueError("tool invocation identity has unsupported schema")
        invocation_id = payload["invocation_id"]
        attempt_ordinal = payload["attempt_ordinal"]
        definition_identity = payload["definition_identity"]
        catalog_generation = payload["catalog_generation"]
        arguments_digest = payload["arguments_digest"]
        owner_id = payload["owner_id"]
        run_id = payload["run_id"]
        if not isinstance(invocation_id, str):
            raise TypeError("invocation_id must be a string")
        if not isinstance(attempt_ordinal, int) or isinstance(attempt_ordinal, bool):
            raise TypeError("attempt_ordinal must be an integer")
        if not isinstance(definition_identity, str):
            raise TypeError("definition_identity must be a string")
        if not isinstance(catalog_generation, int) or isinstance(catalog_generation, bool):
            raise TypeError("catalog_generation must be an integer")
        if not isinstance(arguments_digest, str):
            raise TypeError("arguments_digest must be a string")
        if not isinstance(owner_id, str):
            raise TypeError("owner_id must be a string")
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string")
        return cls(
            invocation_id=ToolInvocationId(invocation_id),
            attempt_ordinal=ToolAttemptOrdinal(attempt_ordinal),
            definition_identity=definition_identity,
            catalog_generation=catalog_generation,
            arguments_digest=arguments_digest,
            owner_id=owner_id,
            run_id=run_id,
        )


def tool_arguments_digest(arguments: Mapping[str, object]) -> str:
    try:
        frozen = freeze_json(arguments, path="tool_arguments")
        encoded = json.dumps(
            thaw_json(frozen),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TypeError("tool arguments must be canonical JSON values") from error
    return f"sha256-{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ToolsetIdentity:
    """Stable identity persisted at Agent session boundaries.

    ``id`` names the logical Toolset, ``version`` is an application-managed
    semantic version for its behavior, and ``protocol`` prevents XML and Native
    definitions from ever being treated as interchangeable during recovery.
    """

    id: str
    version: str
    protocol: CommandProtocol

    def __post_init__(self) -> None:
        normalized_id = self.id.strip()
        normalized_version = self.version.strip()
        if not normalized_id:
            raise ValueError("Toolset identity id must not be empty")
        if not normalized_version:
            raise ValueError("Toolset identity version must not be empty")
        object.__setattr__(self, "id", normalized_id)
        object.__setattr__(self, "version", normalized_version)
        object.__setattr__(self, "protocol", CommandProtocol(self.protocol))

    def to_payload(self) -> dict[str, str]:
        """Return the JSON-native durable representation."""

        return {
            "id": self.id,
            "version": self.version,
            "protocol": self.protocol.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ToolsetIdentity":
        """Reconstruct and validate an identity from a durable payload."""

        id_value = payload.get("id")
        version = payload.get("version")
        protocol = payload.get("protocol")
        if not isinstance(id_value, str):
            raise TypeError("Toolset identity id must be a string")
        if not isinstance(version, str):
            raise TypeError("Toolset identity version must be a string")
        if not isinstance(protocol, str):
            raise TypeError("Toolset identity protocol must be a string")
        return cls(id=id_value, version=version, protocol=CommandProtocol(protocol))


ToolsetManifest = tuple[ToolsetIdentity, ...]


def parse_toolset_manifest(payload: object) -> ToolsetManifest:
    """Validate a JSON-decoded durable Toolset manifest."""

    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise TypeError("Toolset manifest must be a sequence")
    identities: list[ToolsetIdentity] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise TypeError("Toolset manifest entries must be mappings")
        identities.append(ToolsetIdentity.from_payload(item))
    if len({identity.id for identity in identities}) != len(identities):
        raise ValueError("Toolset manifest contains duplicate ids")
    return tuple(identities)


__all__ = [
    "ToolAttemptOrdinal",
    "TOOL_INVOCATION_IDENTITY_SCHEMA",
    "ToolInvocationId",
    "ToolInvocationIdentity",
    "ToolsetIdentity",
    "ToolsetManifest",
    "parse_toolset_manifest",
    "tool_arguments_digest",
]
