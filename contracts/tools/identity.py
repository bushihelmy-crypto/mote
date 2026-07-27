"""Durable, protocol-explicit Toolset identity contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from mote.contracts.tools.protocol import CommandProtocol


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


__all__ = ["ToolsetIdentity", "ToolsetManifest", "parse_toolset_manifest"]
