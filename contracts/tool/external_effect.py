"""External-effect execution state, separate from approval state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from mote.contracts.artifact import ArtifactRef
from mote.contracts.events.envelope import JsonValue, freeze_json
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.contracts.tool.result import FileChange, ToolMedia


class ExternalEffectState(StrEnum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class ToolEffectReceipt:
    receipt_id: str
    identity: ToolInvocationIdentity
    disposition: Literal["succeeded", "failed"]
    provider_evidence: JsonValue
    artifacts: tuple[ArtifactRef, ...]
    media: tuple[ToolMedia, ...]
    file_changes: tuple[FileChange, ...]
    presentation_digest: str

    def __post_init__(self) -> None:
        if type(self.receipt_id) is not str or not self.receipt_id:
            raise ValueError("effect receipt id must be a non-empty string")
        if not isinstance(self.identity, ToolInvocationIdentity):
            raise TypeError("effect receipt identity has the wrong type")
        if self.disposition not in {"succeeded", "failed"}:
            raise ValueError("effect receipt disposition is invalid")
        object.__setattr__(
            self,
            "provider_evidence",
            freeze_json(self.provider_evidence, path="provider evidence"),
        )
        if type(self.artifacts) is not tuple or any(not isinstance(item, ArtifactRef) for item in self.artifacts):
            raise TypeError("effect receipt artifacts must contain ArtifactRef values")
        if type(self.media) is not tuple or any(not isinstance(item, ToolMedia) for item in self.media):
            raise TypeError("effect receipt media must contain ToolMedia values")
        if type(self.file_changes) is not tuple or any(not isinstance(item, FileChange) for item in self.file_changes):
            raise TypeError("effect receipt file_changes must contain FileChange values")
        if type(self.presentation_digest) is not str or not self.presentation_digest:
            raise ValueError("effect receipt presentation digest must be non-empty")


__all__ = ["ExternalEffectState", "ToolEffectReceipt"]
