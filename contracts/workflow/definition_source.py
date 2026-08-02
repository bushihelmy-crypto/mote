"""Durable, callable-free Workflow definition source contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

WORKFLOW_DEFINITION_SOURCE_SCHEMA = "mote.workflow-definition-source/v1"


def _reject_non_finite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _identity(value: str, label: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class DeclarativeWorkflowDefinitionSource:
    compiler_id: str
    compiler_version: int
    payload: str
    payload_digest: str

    def __post_init__(self) -> None:
        _identity(self.compiler_id, "Workflow compiler identity")
        if type(self.compiler_version) is not int or self.compiler_version < 1:
            raise ValueError("Workflow compiler version must be positive")
        if type(self.payload) is not str or not self.payload:
            raise ValueError("declarative Workflow payload must be non-empty")
        try:
            raw = json.loads(self.payload, parse_constant=_reject_non_finite_json)
        except json.JSONDecodeError as exc:
            raise ValueError("declarative Workflow payload is not JSON") from exc
        if type(raw) is not dict:
            raise ValueError("declarative Workflow payload must be an object")
        if json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False) != self.payload:
            raise ValueError("declarative Workflow payload is not canonical JSON")
        expected = hashlib.sha256(self.payload.encode("utf-8")).hexdigest()
        if self.payload_digest != expected:
            raise ValueError("declarative Workflow payload digest mismatch")


@dataclass(frozen=True, slots=True)
class TrustedWorkflowBlueprintSource:
    blueprint_id: str
    blueprint_version: int

    def __post_init__(self) -> None:
        _identity(self.blueprint_id, "Workflow blueprint identity")
        if type(self.blueprint_version) is not int or self.blueprint_version < 1:
            raise ValueError("Workflow blueprint version must be positive")


WorkflowDefinitionSource = DeclarativeWorkflowDefinitionSource | TrustedWorkflowBlueprintSource


def encode_workflow_definition_source(source: WorkflowDefinitionSource) -> dict:
    if isinstance(source, DeclarativeWorkflowDefinitionSource):
        return {
            "schema": WORKFLOW_DEFINITION_SOURCE_SCHEMA,
            "kind": "declarative_spec",
            "compiler_id": source.compiler_id,
            "compiler_version": source.compiler_version,
            "payload": source.payload,
            "payload_digest": source.payload_digest,
        }
    if isinstance(source, TrustedWorkflowBlueprintSource):
        return {
            "schema": WORKFLOW_DEFINITION_SOURCE_SCHEMA,
            "kind": "trusted_blueprint",
            "blueprint_id": source.blueprint_id,
            "blueprint_version": source.blueprint_version,
        }
    raise TypeError("unknown Workflow definition source")


def decode_workflow_definition_source(raw: object) -> WorkflowDefinitionSource:
    if type(raw) is not dict:
        raise ValueError("Workflow definition source must be an object")
    kind = raw.get("kind")
    if kind == "declarative_spec":
        fields = {
            "schema",
            "kind",
            "compiler_id",
            "compiler_version",
            "payload",
            "payload_digest",
        }
        if set(raw) != fields:
            raise ValueError("declarative Workflow definition source shape is invalid")
        if raw["schema"] != WORKFLOW_DEFINITION_SOURCE_SCHEMA:
            raise ValueError("Workflow definition source schema is unknown")
        if any(type(raw[key]) is not str for key in ("compiler_id", "payload", "payload_digest")):
            raise ValueError("declarative Workflow source string primitive is invalid")
        if type(raw["compiler_version"]) is not int:
            raise ValueError("declarative Workflow compiler version is invalid")
        return DeclarativeWorkflowDefinitionSource(
            raw["compiler_id"],
            raw["compiler_version"],
            raw["payload"],
            raw["payload_digest"],
        )
    if kind == "trusted_blueprint":
        fields = {"schema", "kind", "blueprint_id", "blueprint_version"}
        if set(raw) != fields:
            raise ValueError("trusted Workflow blueprint source shape is invalid")
        if raw["schema"] != WORKFLOW_DEFINITION_SOURCE_SCHEMA:
            raise ValueError("Workflow definition source schema is unknown")
        if type(raw["blueprint_id"]) is not str or type(raw["blueprint_version"]) is not int:
            raise ValueError("trusted Workflow blueprint primitive is invalid")
        return TrustedWorkflowBlueprintSource(raw["blueprint_id"], raw["blueprint_version"])
    raise ValueError("Workflow definition source kind is unknown")


__all__ = [
    "DeclarativeWorkflowDefinitionSource",
    "TrustedWorkflowBlueprintSource",
    "WorkflowDefinitionSource",
    "WORKFLOW_DEFINITION_SOURCE_SCHEMA",
    "decode_workflow_definition_source",
    "encode_workflow_definition_source",
]
