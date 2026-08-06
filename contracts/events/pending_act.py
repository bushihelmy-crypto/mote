"""Strict Session-owned facts for durable PendingAct recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal, Mapping, cast

from mote.contracts.artifact import ArtifactRef
from mote.contracts.authorization import RiskLevel
from mote.contracts.events._base import DurableFact
from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    PendingActionArgumentsRevision,
    ToolCompositionDefinitionRef,
)
from mote.contracts.execution.pending_act_claim import PendingActClaimId, PendingActExecutionClaim
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.interaction.approval import (
    ApprovalDisposition,
    ApprovalKind,
    ApprovalReasonCode,
    ApprovalRequest,
    ApprovalState,
)
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.tool.arguments import freeze_tool_arguments
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.external_effect import ExternalEffectState, ToolEffectReceipt
from mote.contracts.tool.identity import ToolInvocationId, ToolInvocationIdentity
from mote.contracts.tool.result import FileChange, ToolMedia

PENDING_ACT_SCHEMA_ACTIVATED = "pending_act_schema_activated.v1"
PENDING_ACT_CREATED = "pending_act_created.v1"
PENDING_ACTION_ARGUMENTS_REVISED = "pending_action_arguments_revised.v1"
APPROVAL_REQUESTED = "approval_requested.v1"
APPROVAL_DECISION_COMMITTED = "approval_decision_committed.v1"
EXTERNAL_EFFECT_STARTED = "external_effect_started.v1"
EXTERNAL_EFFECT_FINISHED = "external_effect_finished.v1"
EXTERNAL_EFFECT_IN_DOUBT = "external_effect_in_doubt.v1"
PENDING_ACT_SETTLED = "pending_act_settled.v1"
PENDING_ACTION_RESULT_COMMITTED = "pending_action_result_committed.v1"
PENDING_ACTIONS_SKIPPED = "pending_actions_skipped.v1"
RUN_RECOVERY_CURSOR_ADVANCED = "run_recovery_cursor_advanced.v1"
PENDING_ACT_CLAIM_ACQUIRED = "pending_act_claim_acquired.v1"
PENDING_ACT_CLAIM_RENEWED = "pending_act_claim_renewed.v1"
PENDING_ACT_CLAIM_TAKEN_OVER = "pending_act_claim_taken_over.v1"
PENDING_ACT_CLAIM_RELEASED = "pending_act_claim_released.v1"
TURN_INTERRUPTED = "turn_interrupted.v1"
TURN_INTERRUPTED_CONTEXT_ATTACHED = "turn_interrupted_context_attached.v1"
TURN_INTERRUPT_SETTLED = "turn_interrupt_settled.v1"
PENDING_ACT_INTERRUPTED = "pending_act_interrupted.v1"


def _exact(payload: dict[str, JsonValue], fields: set[str], owner: str) -> None:
    if set(payload) != fields:
        raise ValueError(f"{owner} payload fields are not canonical")


def _text(payload: Mapping[str, JsonValue], name: str, owner: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"{owner}.{name} must be a string")
    return value


def _integer(payload: Mapping[str, JsonValue], name: str, owner: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"{owner}.{name} must be an integer")
    return value


def _boolean(payload: Mapping[str, JsonValue], name: str, owner: str) -> bool:
    value = payload[name]
    if type(value) is not bool:
        raise TypeError(f"{owner}.{name} must be a boolean")
    return value


def _object(value: JsonValue, owner: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{owner} must be an object")
    return dict(value)


def _definition_payload(value: ToolCompositionDefinitionRef) -> dict[str, JsonValue]:
    return {
        "blueprint_identity": value.blueprint_identity,
        "blueprint_version": value.blueprint_version,
        "executable_digest": value.executable_digest,
        "composition_generation_id": value.composition_generation_id,
        "catalog_fingerprint": value.catalog_fingerprint,
        "provider_descriptor_digest": value.provider_descriptor_digest,
        "policy_generation": value.policy_generation,
        "capability_fingerprint": value.capability_fingerprint,
    }


def _decode_definition(value: JsonValue) -> ToolCompositionDefinitionRef:
    payload = _object(value, "definition_ref")
    fields = {
        "blueprint_identity",
        "blueprint_version",
        "executable_digest",
        "composition_generation_id",
        "catalog_fingerprint",
        "provider_descriptor_digest",
        "policy_generation",
        "capability_fingerprint",
    }
    _exact(payload, fields, "ToolCompositionDefinitionRef")
    return ToolCompositionDefinitionRef(
        **{name: _text(payload, name, "ToolCompositionDefinitionRef") for name in fields}
    )


def _action_payload(value: PendingAction) -> dict[str, JsonValue]:
    return {
        "ordinal": value.ordinal,
        "invocation_id": value.invocation_id.value,
        "action_id": value.action_id,
        "tool_name": value.tool_name,
        "definition_identity": value.definition_identity,
        "catalog_generation": value.catalog_generation,
        "effect": value.effect.value,
        "current_arguments_revision": value.current_arguments_revision,
        "fileops_transaction_id": value.fileops_transaction_id,
    }


def _decode_action(value: JsonValue) -> PendingAction:
    payload = _object(value, "PendingAction")
    fields = {
        "ordinal",
        "invocation_id",
        "action_id",
        "tool_name",
        "definition_identity",
        "catalog_generation",
        "effect",
        "current_arguments_revision",
        "fileops_transaction_id",
    }
    _exact(payload, fields, "PendingAction")
    fileops = payload["fileops_transaction_id"]
    if fileops is not None and type(fileops) is not str:
        raise TypeError("PendingAction.fileops_transaction_id must be a string or null")
    return PendingAction(
        ordinal=_integer(payload, "ordinal", "PendingAction"),
        invocation_id=ToolInvocationId(_text(payload, "invocation_id", "PendingAction")),
        action_id=_text(payload, "action_id", "PendingAction"),
        tool_name=_text(payload, "tool_name", "PendingAction"),
        definition_identity=_text(payload, "definition_identity", "PendingAction"),
        catalog_generation=_integer(payload, "catalog_generation", "PendingAction"),
        effect=ToolEffect(_text(payload, "effect", "PendingAction")),
        current_arguments_revision=_integer(payload, "current_arguments_revision", "PendingAction"),
        fileops_transaction_id=fileops,
    )


def _frontier_payload(value: PendingActFrontier) -> dict[str, JsonValue]:
    return {
        "schema_version": value.schema_version,
        "frontier_id": value.frontier_id.value,
        "session_id": value.session_id,
        "run_id": value.run_id,
        "model_call_id": value.model_call_id,
        "revision": value.revision,
        "definition_ref": _definition_payload(value.definition_ref),
        "actions": [_action_payload(action) for action in value.actions],
    }


def _decode_frontier(value: JsonValue) -> PendingActFrontier:
    payload = _object(value, "PendingActFrontier")
    fields = {
        "schema_version",
        "frontier_id",
        "session_id",
        "run_id",
        "model_call_id",
        "revision",
        "definition_ref",
        "actions",
    }
    _exact(payload, fields, "PendingActFrontier")
    actions = payload["actions"]
    if not isinstance(actions, (list, tuple)):
        raise TypeError("PendingActFrontier.actions must be a list")
    return PendingActFrontier(
        schema_version=cast(Literal[1], _integer(payload, "schema_version", "PendingActFrontier")),
        frontier_id=PendingActFrontierId(_text(payload, "frontier_id", "PendingActFrontier")),
        session_id=_text(payload, "session_id", "PendingActFrontier"),
        run_id=_text(payload, "run_id", "PendingActFrontier"),
        model_call_id=_text(payload, "model_call_id", "PendingActFrontier"),
        revision=_integer(payload, "revision", "PendingActFrontier"),
        definition_ref=_decode_definition(payload["definition_ref"]),
        actions=tuple(_decode_action(action) for action in actions),
    )


@dataclass(frozen=True)
class PendingActSchemaActivatedEvent(DurableFact):
    activated_run_id: str
    type: ClassVar[str] = PENDING_ACT_SCHEMA_ACTIVATED

    def payload(self) -> dict[str, JsonValue]:
        return {"activated_run_id": self.activated_run_id}

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActSchemaActivatedEvent":
        _exact(payload, {"activated_run_id"}, cls.__name__)
        return cls(_text(payload, "activated_run_id", cls.__name__))


@dataclass(frozen=True)
class PendingActCreatedEvent(DurableFact):
    frontier: PendingActFrontier
    type: ClassVar[str] = PENDING_ACT_CREATED

    def payload(self) -> dict[str, JsonValue]:
        return {"frontier": _frontier_payload(self.frontier)}

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActCreatedEvent":
        _exact(payload, {"frontier"}, cls.__name__)
        return cls(_decode_frontier(payload["frontier"]))


@dataclass(frozen=True)
class PendingActionArgumentsRevisedEvent(DurableFact):
    frontier_id: PendingActFrontierId
    revision: PendingActionArgumentsRevision
    previous_arguments_digest: str | None
    type: ClassVar[str] = PENDING_ACTION_ARGUMENTS_REVISED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "invocation_id": self.revision.invocation_id.value,
            "revision": self.revision.revision,
            "arguments": cast(JsonValue, thaw_json(cast(JsonValue, self.revision.arguments))),
            "arguments_digest": self.revision.arguments_digest,
            "previous_arguments_digest": self.previous_arguments_digest,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActionArgumentsRevisedEvent":
        fields = {
            "frontier_id",
            "invocation_id",
            "revision",
            "arguments",
            "arguments_digest",
            "previous_arguments_digest",
        }
        _exact(payload, fields, cls.__name__)
        previous = payload["previous_arguments_digest"]
        if previous is not None and type(previous) is not str:
            raise TypeError("previous_arguments_digest must be a string or null")
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            PendingActionArgumentsRevision(
                ToolInvocationId(_text(payload, "invocation_id", cls.__name__)),
                _integer(payload, "revision", cls.__name__),
                freeze_tool_arguments(payload["arguments"]),
                _text(payload, "arguments_digest", cls.__name__),
            ),
            previous,
        )


@dataclass(frozen=True)
class ApprovalRequestedEvent(DurableFact):
    request: ApprovalRequest
    type: ClassVar[str] = APPROVAL_REQUESTED

    def __post_init__(self) -> None:
        if self.request.request_id is None or self.request.frontier_id is None or self.request.invocation_id is None:
            raise ValueError("durable approval request identity is incomplete")

    def payload(self) -> dict[str, JsonValue]:
        request = self.request
        assert request.request_id is not None and request.frontier_id is not None and request.invocation_id is not None
        return {
            "request_id": request.request_id.value,
            "frontier_id": request.frontier_id.value,
            "invocation_id": request.invocation_id.value,
            "arguments_revision": request.arguments_revision,
            "arguments_digest": request.arguments_digest,
            "permission_targets_digest": request.permission_targets_digest,
            "expected_frontier_revision": request.expected_frontier_revision,
            "tool_name": request.tool_name,
            "kind": request.kind,
            "target": request.target,
            "paths": list(request.paths),
            "risk": request.risk,
            "reason_code": request.reason_code,
            "reason_detail": request.reason_detail,
            "suggestion": request.suggestion,
            "mutates_fs": request.mutates_fs,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "ApprovalRequestedEvent":
        fields = {
            "request_id",
            "frontier_id",
            "invocation_id",
            "arguments_revision",
            "arguments_digest",
            "permission_targets_digest",
            "expected_frontier_revision",
            "tool_name",
            "kind",
            "target",
            "paths",
            "risk",
            "reason_code",
            "reason_detail",
            "suggestion",
            "mutates_fs",
        }
        _exact(payload, fields, cls.__name__)
        paths = payload["paths"]
        if not isinstance(paths, (list, tuple)) or any(type(path) is not str for path in paths):
            raise TypeError("approval paths must be a list of strings")
        return cls(
            ApprovalRequest(
                tool_name=_text(payload, "tool_name", cls.__name__),
                kind=cast(ApprovalKind, _text(payload, "kind", cls.__name__)),
                target=_text(payload, "target", cls.__name__),
                paths=[cast(str, path) for path in paths],
                risk=cast(RiskLevel, _text(payload, "risk", cls.__name__)),
                reason_code=cast(ApprovalReasonCode, _text(payload, "reason_code", cls.__name__)),
                reason_detail=_text(payload, "reason_detail", cls.__name__),
                suggestion=_text(payload, "suggestion", cls.__name__),
                mutates_fs=_boolean(payload, "mutates_fs", cls.__name__),
                request_id=ApprovalRequestId(_text(payload, "request_id", cls.__name__)),
                frontier_id=PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
                invocation_id=ToolInvocationId(_text(payload, "invocation_id", cls.__name__)),
                arguments_revision=_integer(payload, "arguments_revision", cls.__name__),
                arguments_digest=_text(payload, "arguments_digest", cls.__name__),
                permission_targets_digest=_text(payload, "permission_targets_digest", cls.__name__),
                expected_frontier_revision=_integer(payload, "expected_frontier_revision", cls.__name__),
            )
        )


@dataclass(frozen=True)
class ApprovalDecisionCommittedEvent(DurableFact):
    request_id: ApprovalRequestId
    disposition: ApprovalDisposition
    arguments_revision: int
    arguments_digest: str
    type: ClassVar[str] = APPROVAL_DECISION_COMMITTED

    @property
    def state(self) -> ApprovalState:
        if self.disposition in {
            ApprovalDisposition.ALLOW_ONCE,
            ApprovalDisposition.ALLOW_SESSION,
        }:
            return ApprovalState.APPROVED
        return ApprovalState.REJECTED if self.disposition is ApprovalDisposition.REJECT else ApprovalState.CANCELLED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id.value,
            "disposition": self.disposition.value,
            "arguments_revision": self.arguments_revision,
            "arguments_digest": self.arguments_digest,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "ApprovalDecisionCommittedEvent":
        _exact(
            payload,
            {"request_id", "disposition", "arguments_revision", "arguments_digest"},
            cls.__name__,
        )
        return cls(
            ApprovalRequestId(_text(payload, "request_id", cls.__name__)),
            ApprovalDisposition(_text(payload, "disposition", cls.__name__)),
            _integer(payload, "arguments_revision", cls.__name__),
            _text(payload, "arguments_digest", cls.__name__),
        )


@dataclass(frozen=True)
class SessionPermissionRuleGrantedEvent(DurableFact):
    request_id: ApprovalRequestId
    tool_name: str
    permission_targets: tuple[str, ...]
    mutates_fs: bool
    type: ClassVar[str] = "session.permission_rule.granted.v1"

    def payload(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id.value,
            "tool_name": self.tool_name,
            "permission_targets": list(self.permission_targets),
            "mutates_fs": self.mutates_fs,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "SessionPermissionRuleGrantedEvent":
        _exact(
            payload,
            {"request_id", "tool_name", "permission_targets", "mutates_fs"},
            cls.__name__,
        )
        targets = payload["permission_targets"]
        if not isinstance(targets, (list, tuple)) or any(type(target) is not str for target in targets):
            raise TypeError("permission targets must be a list of strings")
        mutates_fs = payload["mutates_fs"]
        if type(mutates_fs) is not bool:
            raise TypeError("mutates_fs must be a boolean")
        return cls(
            ApprovalRequestId(_text(payload, "request_id", cls.__name__)),
            _text(payload, "tool_name", cls.__name__),
            tuple(cast(str, target) for target in targets),
            mutates_fs,
        )


@dataclass(frozen=True)
class ExternalEffectStartedEvent(DurableFact):
    frontier_id: PendingActFrontierId
    identity: ToolInvocationIdentity
    approval_request_id: ApprovalRequestId | None
    frontier_revision: int
    claim_fencing_token: int
    type: ClassVar[str] = EXTERNAL_EFFECT_STARTED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "identity": cast(JsonValue, self.identity.to_payload()),
            "approval_request_id": (self.approval_request_id.value if self.approval_request_id else None),
            "frontier_revision": self.frontier_revision,
            "claim_fencing_token": self.claim_fencing_token,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "ExternalEffectStartedEvent":
        _exact(
            payload,
            {
                "frontier_id",
                "identity",
                "approval_request_id",
                "frontier_revision",
                "claim_fencing_token",
            },
            cls.__name__,
        )
        approval = payload["approval_request_id"]
        if approval is not None and type(approval) is not str:
            raise TypeError("approval_request_id must be a string or null")
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            ToolInvocationIdentity.from_payload(_object(payload["identity"], "identity")),
            ApprovalRequestId(approval) if approval else None,
            _integer(payload, "frontier_revision", cls.__name__),
            _integer(payload, "claim_fencing_token", cls.__name__),
        )


@dataclass(frozen=True)
class ExternalEffectFinishedEvent(DurableFact):
    frontier_id: PendingActFrontierId
    receipt: ToolEffectReceipt
    type: ClassVar[str] = EXTERNAL_EFFECT_FINISHED

    def __post_init__(self) -> None:
        if self.receipt.disposition not in {"succeeded", "failed"}:
            raise ValueError("finished external effect must be succeeded or failed")

    @property
    def invocation_id(self) -> ToolInvocationId:
        return self.receipt.identity.invocation_id

    @property
    def disposition(self) -> ExternalEffectState:
        return ExternalEffectState(self.receipt.disposition)

    def payload(self) -> dict[str, JsonValue]:
        receipt = self.receipt
        return {
            "frontier_id": self.frontier_id.value,
            "receipt": {
                "receipt_id": receipt.receipt_id,
                "identity": cast(JsonValue, receipt.identity.to_payload()),
                "disposition": receipt.disposition,
                "provider_evidence": receipt.provider_evidence,
                "artifacts": [cast(JsonValue, item.to_dict()) for item in receipt.artifacts],
                "media": [
                    {
                        "artifact": cast(JsonValue, item.artifact.to_dict()),
                        "kind": item.kind,
                        "ref": item.ref,
                        "mime": item.mime,
                    }
                    for item in receipt.media
                ],
                "file_changes": [
                    {
                        "path": item.path,
                        "old": item.old,
                        "new": item.new,
                        "transaction_id": item.transaction_id,
                        "post_digest": item.post_digest,
                    }
                    for item in receipt.file_changes
                ],
                "presentation_digest": receipt.presentation_digest,
            },
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "ExternalEffectFinishedEvent":
        _exact(payload, {"frontier_id", "receipt"}, cls.__name__)
        receipt = _object(payload["receipt"], "ToolEffectReceipt")
        _exact(
            receipt,
            {
                "receipt_id",
                "identity",
                "disposition",
                "provider_evidence",
                "artifacts",
                "media",
                "file_changes",
                "presentation_digest",
            },
            "ToolEffectReceipt",
        )
        artifacts = receipt["artifacts"]
        media = receipt["media"]
        changes = receipt["file_changes"]
        if not isinstance(artifacts, (list, tuple)):
            raise TypeError("effect receipt artifacts must be a list")
        if not isinstance(media, (list, tuple)):
            raise TypeError("effect receipt media must be a list")
        if not isinstance(changes, (list, tuple)):
            raise TypeError("effect receipt file_changes must be a list")
        decoded_media: list[ToolMedia] = []
        for value in media:
            item = _object(value, "ToolMedia")
            _exact(item, {"artifact", "kind", "ref", "mime"}, "ToolMedia")
            mime = item["mime"]
            if mime is not None and type(mime) is not str:
                raise TypeError("ToolMedia.mime must be a string or null")
            decoded_media.append(
                ToolMedia(
                    ArtifactRef.from_dict(thaw_json(item["artifact"])),
                    _text(item, "kind", "ToolMedia"),
                    _text(item, "ref", "ToolMedia"),
                    mime,
                )
            )
        decoded_changes: list[FileChange] = []
        for value in changes:
            item = _object(value, "FileChange")
            _exact(
                item,
                {"path", "old", "new", "transaction_id", "post_digest"},
                "FileChange",
            )
            decoded_changes.append(FileChange(**{name: _text(item, name, "FileChange") for name in item}))
        disposition = _text(receipt, "disposition", "ToolEffectReceipt")
        if disposition not in {"succeeded", "failed"}:
            raise ValueError("ToolEffectReceipt disposition is invalid")
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            ToolEffectReceipt(
                _text(receipt, "receipt_id", "ToolEffectReceipt"),
                ToolInvocationIdentity.from_payload(_object(receipt["identity"], "identity")),
                cast(Literal["succeeded", "failed"], disposition),
                receipt["provider_evidence"],
                tuple(ArtifactRef.from_dict(thaw_json(value)) for value in artifacts),
                tuple(decoded_media),
                tuple(decoded_changes),
                _text(receipt, "presentation_digest", "ToolEffectReceipt"),
            ),
        )


@dataclass(frozen=True)
class ExternalEffectInDoubtEvent(DurableFact):
    frontier_id: PendingActFrontierId
    invocation_id: ToolInvocationId
    evidence: JsonValue
    type: ClassVar[str] = EXTERNAL_EFFECT_IN_DOUBT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence",
            freeze_json(self.evidence, path="external effect evidence"),
        )

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "invocation_id": self.invocation_id.value,
            "evidence": self.evidence,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "ExternalEffectInDoubtEvent":
        _exact(payload, {"frontier_id", "invocation_id", "evidence"}, cls.__name__)
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            ToolInvocationId(_text(payload, "invocation_id", cls.__name__)),
            payload["evidence"],
        )


@dataclass(frozen=True)
class PendingActSettledEvent(DurableFact):
    frontier_id: PendingActFrontierId
    final_revision: int
    type: ClassVar[str] = PENDING_ACT_SETTLED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "final_revision": self.final_revision,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActSettledEvent":
        _exact(payload, {"frontier_id", "final_revision"}, cls.__name__)
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            _integer(payload, "final_revision", cls.__name__),
        )


@dataclass(frozen=True)
class TurnInterruptedEvent(DurableFact):
    run_id: str
    model_call_id: str | None
    reason: Literal["user_interrupted"]
    interrupted_at: datetime
    type: ClassVar[str] = TURN_INTERRUPTED

    def __post_init__(self) -> None:
        if not self.run_id or self.interrupted_at.tzinfo is None:
            raise ValueError("turn interrupt identity and instant are required")

    def payload(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "model_call_id": self.model_call_id,
            "reason": self.reason,
            "interrupted_at": self.interrupted_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "TurnInterruptedEvent":
        _exact(
            payload,
            {"run_id", "model_call_id", "reason", "interrupted_at"},
            cls.__name__,
        )
        model_call_id = payload["model_call_id"]
        if model_call_id is not None and type(model_call_id) is not str:
            raise TypeError("model_call_id must be a string or null")
        reason = _text(payload, "reason", cls.__name__)
        if reason != "user_interrupted":
            raise ValueError("turn interrupt reason is invalid")
        return cls(
            _text(payload, "run_id", cls.__name__),
            model_call_id,
            "user_interrupted",
            datetime.fromisoformat(_text(payload, "interrupted_at", cls.__name__)),
        )


@dataclass(frozen=True)
class PendingActInterruptedEvent(DurableFact):
    frontier_id: PendingActFrontierId
    final_revision: int
    type: ClassVar[str] = PENDING_ACT_INTERRUPTED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "final_revision": self.final_revision,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActInterruptedEvent":
        _exact(payload, {"frontier_id", "final_revision"}, cls.__name__)
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            _integer(payload, "final_revision", cls.__name__),
        )


@dataclass(frozen=True)
class TurnInterruptedContextAttachedEvent(DurableFact):
    run_id: str
    anchor_message_id: str
    type: ClassVar[str] = TURN_INTERRUPTED_CONTEXT_ATTACHED

    def payload(self) -> dict[str, JsonValue]:
        return {"run_id": self.run_id, "anchor_message_id": self.anchor_message_id}

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "TurnInterruptedContextAttachedEvent":
        _exact(payload, {"run_id", "anchor_message_id"}, cls.__name__)
        return cls(
            _text(payload, "run_id", cls.__name__),
            _text(payload, "anchor_message_id", cls.__name__),
        )


@dataclass(frozen=True)
class TurnInterruptSettledEvent(DurableFact):
    run_id: str
    type: ClassVar[str] = TURN_INTERRUPT_SETTLED

    def payload(self) -> dict[str, JsonValue]:
        return {"run_id": self.run_id}

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "TurnInterruptSettledEvent":
        _exact(payload, {"run_id"}, cls.__name__)
        return cls(_text(payload, "run_id", cls.__name__))


@dataclass(frozen=True)
class PendingActionResultCommittedEvent(DurableFact):
    frontier_id: PendingActFrontierId
    invocation_id: ToolInvocationId
    message_id: str
    receipt_id: str | None = None
    presentation_digest: str | None = None
    type: ClassVar[str] = PENDING_ACTION_RESULT_COMMITTED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "invocation_id": self.invocation_id.value,
            "message_id": self.message_id,
            "receipt_id": self.receipt_id,
            "presentation_digest": self.presentation_digest,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActionResultCommittedEvent":
        _exact(
            payload,
            {
                "frontier_id",
                "invocation_id",
                "message_id",
                "receipt_id",
                "presentation_digest",
            },
            cls.__name__,
        )
        receipt_id = payload["receipt_id"]
        digest = payload["presentation_digest"]
        if receipt_id is not None and type(receipt_id) is not str:
            raise TypeError("receipt_id must be a string or null")
        if digest is not None and type(digest) is not str:
            raise TypeError("presentation_digest must be a string or null")
        if (receipt_id is None) != (digest is None):
            raise ValueError("receipt identity and digest must be present together")
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            ToolInvocationId(_text(payload, "invocation_id", cls.__name__)),
            _text(payload, "message_id", cls.__name__),
            receipt_id,
            digest,
        )


@dataclass(frozen=True)
class PendingActionsSkippedEvent(DurableFact):
    frontier_id: PendingActFrontierId
    invocation_ids: tuple[ToolInvocationId, ...]
    reason: str
    type: ClassVar[str] = PENDING_ACTIONS_SKIPPED

    def __post_init__(self) -> None:
        if not self.invocation_ids or len(set(self.invocation_ids)) != len(self.invocation_ids):
            raise ValueError("skipped action identities must be non-empty and unique")

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "invocation_ids": [item.value for item in self.invocation_ids],
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActionsSkippedEvent":
        _exact(payload, {"frontier_id", "invocation_ids", "reason"}, cls.__name__)
        invocation_ids = payload["invocation_ids"]
        if not isinstance(invocation_ids, (list, tuple)) or any(type(item) is not str for item in invocation_ids):
            raise TypeError("invocation_ids must be a list of strings")
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            tuple(ToolInvocationId(cast(str, item)) for item in invocation_ids),
            _text(payload, "reason", cls.__name__),
        )


@dataclass(frozen=True)
class RunRecoveryCursorAdvancedEvent(DurableFact):
    cursor: RunRecoveryCursor
    type: ClassVar[str] = RUN_RECOVERY_CURSOR_ADVANCED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.cursor.run_id,
            "revision": self.cursor.revision,
            "next_node": self.cursor.next_node.value,
            "pending_act_id": (self.cursor.pending_act_id.value if self.cursor.pending_act_id else None),
            "continue_inference": self.cursor.continue_inference,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "RunRecoveryCursorAdvancedEvent":
        _exact(
            payload,
            {"run_id", "revision", "next_node", "pending_act_id", "continue_inference"},
            cls.__name__,
        )
        pending = payload["pending_act_id"]
        if pending is not None and type(pending) is not str:
            raise TypeError("pending_act_id must be a string or null")
        return cls(
            RunRecoveryCursor(
                _text(payload, "run_id", cls.__name__),
                _integer(payload, "revision", cls.__name__),
                RecoveryTarget(_text(payload, "next_node", cls.__name__)),
                PendingActFrontierId(pending) if pending else None,
                _boolean(payload, "continue_inference", cls.__name__),
            )
        )


def _claim_payload(claim: PendingActExecutionClaim) -> dict[str, JsonValue]:
    return {
        "claim_id": claim.claim_id.value,
        "frontier_id": claim.frontier_id.value,
        "owner_id": claim.owner_id,
        "incarnation_id": claim.incarnation_id,
        "claim_revision": claim.claim_revision,
        "fencing_token": claim.fencing_token,
        "acquired_at": claim.acquired_at.isoformat(),
        "expires_at": claim.expires_at.isoformat(),
    }


def _decode_claim(value: JsonValue) -> PendingActExecutionClaim:
    payload = _object(value, "PendingActExecutionClaim")
    _exact(
        payload,
        {
            "claim_id",
            "frontier_id",
            "owner_id",
            "incarnation_id",
            "claim_revision",
            "fencing_token",
            "acquired_at",
            "expires_at",
        },
        "PendingActExecutionClaim",
    )
    return PendingActExecutionClaim(
        PendingActClaimId(_text(payload, "claim_id", "PendingActExecutionClaim")),
        PendingActFrontierId(_text(payload, "frontier_id", "PendingActExecutionClaim")),
        _text(payload, "owner_id", "PendingActExecutionClaim"),
        _text(payload, "incarnation_id", "PendingActExecutionClaim"),
        _integer(payload, "claim_revision", "PendingActExecutionClaim"),
        _integer(payload, "fencing_token", "PendingActExecutionClaim"),
        datetime.fromisoformat(_text(payload, "acquired_at", "PendingActExecutionClaim")),
        datetime.fromisoformat(_text(payload, "expires_at", "PendingActExecutionClaim")),
    )


@dataclass(frozen=True)
class PendingActClaimAcquiredEvent(DurableFact):
    claim: PendingActExecutionClaim
    type: ClassVar[str] = PENDING_ACT_CLAIM_ACQUIRED

    def payload(self) -> dict[str, JsonValue]:
        return {"claim": _claim_payload(self.claim)}

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActClaimAcquiredEvent":
        _exact(payload, {"claim"}, cls.__name__)
        return cls(_decode_claim(payload["claim"]))


@dataclass(frozen=True)
class PendingActClaimRenewedEvent(PendingActClaimAcquiredEvent):
    type: ClassVar[str] = PENDING_ACT_CLAIM_RENEWED


@dataclass(frozen=True)
class PendingActClaimTakenOverEvent(PendingActClaimAcquiredEvent):
    type: ClassVar[str] = PENDING_ACT_CLAIM_TAKEN_OVER


@dataclass(frozen=True)
class PendingActClaimReleasedEvent(DurableFact):
    frontier_id: PendingActFrontierId
    claim_id: PendingActClaimId
    claim_revision: int
    fencing_token: int
    type: ClassVar[str] = PENDING_ACT_CLAIM_RELEASED

    def payload(self) -> dict[str, JsonValue]:
        return {
            "frontier_id": self.frontier_id.value,
            "claim_id": self.claim_id.value,
            "claim_revision": self.claim_revision,
            "fencing_token": self.fencing_token,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PendingActClaimReleasedEvent":
        _exact(
            payload,
            {"frontier_id", "claim_id", "claim_revision", "fencing_token"},
            cls.__name__,
        )
        return cls(
            PendingActFrontierId(_text(payload, "frontier_id", cls.__name__)),
            PendingActClaimId(_text(payload, "claim_id", cls.__name__)),
            _integer(payload, "claim_revision", cls.__name__),
            _integer(payload, "fencing_token", cls.__name__),
        )


PendingActEvent = (
    PendingActSchemaActivatedEvent
    | PendingActCreatedEvent
    | PendingActionArgumentsRevisedEvent
    | ApprovalRequestedEvent
    | ApprovalDecisionCommittedEvent
    | SessionPermissionRuleGrantedEvent
    | ExternalEffectStartedEvent
    | ExternalEffectFinishedEvent
    | ExternalEffectInDoubtEvent
    | PendingActionResultCommittedEvent
    | PendingActionsSkippedEvent
    | PendingActSettledEvent
    | TurnInterruptedEvent
    | PendingActInterruptedEvent
    | TurnInterruptedContextAttachedEvent
    | TurnInterruptSettledEvent
    | RunRecoveryCursorAdvancedEvent
    | PendingActClaimAcquiredEvent
    | PendingActClaimRenewedEvent
    | PendingActClaimTakenOverEvent
    | PendingActClaimReleasedEvent
)

__all__ = [
    "APPROVAL_DECISION_COMMITTED",
    "APPROVAL_REQUESTED",
    "EXTERNAL_EFFECT_FINISHED",
    "EXTERNAL_EFFECT_IN_DOUBT",
    "EXTERNAL_EFFECT_STARTED",
    "PENDING_ACTION_ARGUMENTS_REVISED",
    "PENDING_ACT_CREATED",
    "PENDING_ACT_SCHEMA_ACTIVATED",
    "PENDING_ACT_SETTLED",
    "PENDING_ACTION_RESULT_COMMITTED",
    "PENDING_ACTIONS_SKIPPED",
    "RUN_RECOVERY_CURSOR_ADVANCED",
    "PENDING_ACT_CLAIM_ACQUIRED",
    "PENDING_ACT_CLAIM_RENEWED",
    "PENDING_ACT_CLAIM_TAKEN_OVER",
    "PENDING_ACT_CLAIM_RELEASED",
    "TURN_INTERRUPTED",
    "TURN_INTERRUPTED_CONTEXT_ATTACHED",
    "TURN_INTERRUPT_SETTLED",
    "PENDING_ACT_INTERRUPTED",
    "ApprovalDecisionCommittedEvent",
    "SessionPermissionRuleGrantedEvent",
    "ApprovalRequestedEvent",
    "ExternalEffectFinishedEvent",
    "ExternalEffectInDoubtEvent",
    "ExternalEffectStartedEvent",
    "PendingActionArgumentsRevisedEvent",
    "PendingActCreatedEvent",
    "PendingActEvent",
    "PendingActSchemaActivatedEvent",
    "PendingActSettledEvent",
    "PendingActionResultCommittedEvent",
    "PendingActionsSkippedEvent",
    "RunRecoveryCursorAdvancedEvent",
    "PendingActClaimAcquiredEvent",
    "PendingActClaimRenewedEvent",
    "PendingActClaimTakenOverEvent",
    "PendingActClaimReleasedEvent",
    "TurnInterruptedEvent",
    "TurnInterruptedContextAttachedEvent",
    "TurnInterruptSettledEvent",
    "PendingActInterruptedEvent",
]
