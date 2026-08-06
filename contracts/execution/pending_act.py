"""Canonical immutable identities for one durable Act batch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mote.contracts.execution.pending_act_identity import PendingActFrontierId
from mote.contracts.tool.arguments import ToolArguments, freeze_tool_arguments
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.identity import ToolInvocationId, tool_arguments_digest


@dataclass(frozen=True, slots=True)
class PendingActionArgumentsRevision:
    invocation_id: ToolInvocationId
    revision: int
    arguments: ToolArguments
    arguments_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.invocation_id, ToolInvocationId):
            raise TypeError("argument revision requires a ToolInvocationId")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("argument revision must be a non-negative integer")
        frozen = freeze_tool_arguments(self.arguments)
        if tool_arguments_digest(frozen) != self.arguments_digest:
            raise ValueError("argument revision digest does not match its arguments")
        object.__setattr__(self, "arguments", frozen)


@dataclass(frozen=True, slots=True)
class PendingAction:
    ordinal: int
    invocation_id: ToolInvocationId
    action_id: str
    tool_name: str
    definition_identity: str
    catalog_generation: int
    effect: ToolEffect
    current_arguments_revision: int
    fileops_transaction_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("pending action ordinal must be non-negative")
        if not isinstance(self.invocation_id, ToolInvocationId):
            raise TypeError("pending action requires a ToolInvocationId")
        values = {
            "action_id": self.action_id,
            "tool_name": self.tool_name,
            "definition_identity": self.definition_identity,
        }
        for name, value in values.items():
            if type(value) is not str or not value:
                raise ValueError(f"pending action {name} must be a non-empty string")
        if type(self.catalog_generation) is not int or self.catalog_generation < 1:
            raise ValueError("pending action catalog generation must be positive")
        if not isinstance(self.effect, ToolEffect):
            raise TypeError("pending action effect must be ToolEffect")
        if type(self.current_arguments_revision) is not int or self.current_arguments_revision < 0:
            raise ValueError("pending action argument revision must be non-negative")
        if self.fileops_transaction_id is not None and (
            type(self.fileops_transaction_id) is not str or not self.fileops_transaction_id
        ):
            raise ValueError("pending action fileops_transaction_id must be a non-empty string or null")
        if self.effect is not ToolEffect.LOCAL and self.fileops_transaction_id is not None:
            raise ValueError("only LOCAL actions may reference a FileOps transaction")
        if self.effect is ToolEffect.LOCAL and self.fileops_transaction_id is None:
            raise ValueError("LOCAL action requires a FileOps transaction identity")


@dataclass(frozen=True, slots=True)
class ToolCompositionDefinitionRef:
    """Verifiable definition inputs retained for exact binding reconstruction."""

    blueprint_identity: str
    blueprint_version: str
    executable_digest: str
    composition_generation_id: str
    catalog_fingerprint: str
    provider_descriptor_digest: str
    policy_generation: str
    capability_fingerprint: str

    def __post_init__(self) -> None:
        values = {
            "blueprint_identity": self.blueprint_identity,
            "blueprint_version": self.blueprint_version,
            "executable_digest": self.executable_digest,
            "composition_generation_id": self.composition_generation_id,
            "catalog_fingerprint": self.catalog_fingerprint,
            "provider_descriptor_digest": self.provider_descriptor_digest,
            "policy_generation": self.policy_generation,
            "capability_fingerprint": self.capability_fingerprint,
        }
        for name, value in values.items():
            if type(value) is not str or not value:
                raise ValueError(f"tool composition {name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class PendingActFrontier:
    schema_version: Literal[1]
    frontier_id: PendingActFrontierId
    session_id: str
    run_id: str
    model_call_id: str
    revision: int
    definition_ref: ToolCompositionDefinitionRef
    actions: tuple[PendingAction, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported PendingAct schema version")
        if not isinstance(self.frontier_id, PendingActFrontierId):
            raise TypeError("frontier_id must be PendingActFrontierId")
        values = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "model_call_id": self.model_call_id,
        }
        for name, value in values.items():
            if type(value) is not str or not value:
                raise ValueError(f"PendingAct {name} must be a non-empty string")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("PendingAct revision must be non-negative")
        if not isinstance(self.definition_ref, ToolCompositionDefinitionRef):
            raise TypeError("PendingAct definition_ref has the wrong type")
        if type(self.actions) is not tuple or not self.actions:
            raise ValueError("PendingAct must contain at least one action")
        if any(not isinstance(action, PendingAction) for action in self.actions):
            raise TypeError("PendingAct actions must contain PendingAction values")
        if tuple(action.ordinal for action in self.actions) != tuple(range(len(self.actions))):
            raise ValueError("PendingAct action ordinals must be contiguous from zero")
        invocation_ids = tuple(action.invocation_id for action in self.actions)
        action_ids = tuple(action.action_id for action in self.actions)
        if len(set(invocation_ids)) != len(invocation_ids) or len(set(action_ids)) != len(action_ids):
            raise ValueError("PendingAct action identities must be unique")


__all__ = [
    "PendingActFrontier",
    "PendingActFrontierId",
    "PendingAction",
    "PendingActionArgumentsRevision",
    "ToolCompositionDefinitionRef",
]
