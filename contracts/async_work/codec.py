"""Strict wire codec for async-work identities.

Observation payload codecs can only be added with a real wire consumer.  This
module owns the identity envelope shared by command and observation surfaces.
"""

from __future__ import annotations

from collections.abc import Mapping

from mote.contracts.async_work.identity import (
    AsyncWorkKind,
    AsyncWorkReference,
    DurableWorkflowRunReference,
    LocalBackgroundTaskReference,
)
from mote.contracts.async_work.observation import (
    AsyncWorkAction,
    AsyncWorkObservation,
    AsyncWorkPresentationPhase,
    DurableWorkflowObservationDetail,
    DurableWorkflowRunObservation,
    LocalBackgroundObservationDetail,
    LocalBackgroundTaskObservation,
    WorkflowPauseDetail,
    WorkflowPausePresentationReason,
    WorkflowTerminalDeliveryObservation,
    WorkflowTerminalDeliveryState,
)
from mote.contracts.clock import AbsoluteInstant
from mote.contracts.task.codec import decode_task_result_pointer, encode_task_result_pointer
from mote.contracts.task.lifecycle import BackgroundTaskOwner, LocalTaskReference
from mote.contracts.task.models import AttemptId, TaskId
from mote.contracts.workflow.codec import decode_workflow_terminal_result, encode_workflow_terminal_result
from mote.contracts.workflow.identity import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference

ASYNC_WORK_SCHEMA = "mote.async-work-observation/v1"


def encode_async_work_reference(reference: AsyncWorkReference) -> dict[str, object]:
    if isinstance(reference, LocalBackgroundTaskReference):
        local = reference.reference
        return {
            "schema": ASYNC_WORK_SCHEMA,
            "kind": reference.kind.value,
            "payload": {
                "process_instance_id": local.owner.process_instance_id,
                "agent_id": local.owner.agent_id,
                "incarnation_id": local.owner.incarnation_id,
                "task_id": str(local.task_id),
                "attempt_id": local.attempt_id.value,
            },
        }
    if isinstance(reference, DurableWorkflowRunReference):
        workflow = reference.reference
        return {
            "schema": ASYNC_WORK_SCHEMA,
            "kind": reference.kind.value,
            "payload": {
                "run_id": str(workflow.run_id),
                "definition_id": str(workflow.definition_id),
            },
        }
    raise TypeError("unsupported async-work reference variant")


def decode_async_work_reference(raw: Mapping[str, object]) -> AsyncWorkReference:
    if type(raw) is not dict or set(raw) != {"schema", "kind", "payload"}:
        raise ValueError("async-work reference envelope shape is invalid")
    if raw["schema"] != ASYNC_WORK_SCHEMA:
        raise ValueError("async-work reference schema is unsupported")
    if type(raw["kind"]) is not str or type(raw["payload"]) is not dict:
        raise TypeError("async-work reference discriminator and payload are invalid")
    kind = AsyncWorkKind(raw["kind"])
    payload = raw["payload"]
    if kind is AsyncWorkKind.LOCAL_BACKGROUND_TASK:
        fields = {
            "process_instance_id",
            "agent_id",
            "incarnation_id",
            "task_id",
            "attempt_id",
        }
        if set(payload) != fields:
            raise ValueError("local async-work reference shape is invalid")
        for field in fields - {"attempt_id"}:
            if type(payload[field]) is not str or not payload[field]:
                raise TypeError("local async-work identity field is invalid")
        if type(payload["attempt_id"]) is not int:
            raise TypeError("local async-work attempt identity is invalid")
        owner = BackgroundTaskOwner(
            payload["process_instance_id"],
            payload["agent_id"],
            payload["incarnation_id"],
        )
        return LocalBackgroundTaskReference(
            LocalTaskReference(
                owner,
                TaskId(payload["task_id"]),
                AttemptId(payload["attempt_id"]),
            )
        )
    fields = {"run_id", "definition_id"}
    if set(payload) != fields:
        raise ValueError("durable async-work reference shape is invalid")
    for field in fields:
        if type(payload[field]) is not str or not payload[field]:
            raise TypeError("durable async-work identity field is invalid")
    return DurableWorkflowRunReference(
        WorkflowRunReference(
            WorkflowRunId(payload["run_id"]),
            WorkflowDefinitionId(payload["definition_id"]),
        )
    )


def encode_async_work_observation(value: AsyncWorkObservation) -> dict[str, object]:
    common: dict[str, object] = {
        "schema": ASYNC_WORK_SCHEMA,
        "kind": value.reference.kind.value,
        "reference": encode_async_work_reference(value.reference),
        "phase": value.phase.value,
        "available_actions": [action.value for action in value.available_actions],
    }
    if isinstance(value, LocalBackgroundTaskObservation):
        return {
            **common,
            "detail": {
                "label": value.detail.label,
                "owner_available": value.detail.owner_available,
                "pinned": value.detail.pinned,
            },
            "result_pointer": (
                None if value.result_pointer is None else encode_task_result_pointer(value.result_pointer)
            ),
        }
    if isinstance(value, DurableWorkflowRunObservation):
        pause = value.detail.pause
        return {
            **common,
            "revision": value.revision,
            "detail": {
                "pause": (
                    None
                    if pause is None
                    else {
                        "reason": pause.reason.value,
                        "resume_nonce": pause.resume_nonce,
                    }
                ),
            },
            "frontier": list(value.frontier),
            "deadline": None if value.deadline is None else value.deadline.to_dict(),
            "terminal_result": (
                None if value.terminal_result is None else encode_workflow_terminal_result(value.terminal_result)
            ),
            "deliveries": [
                {
                    "delivery_id": item.delivery_id,
                    "destination_id": item.destination_id,
                    "revision": item.revision,
                    "state": item.state.value,
                    "attempts": item.attempts,
                    "next_eligible_at": None if item.next_eligible_at is None else item.next_eligible_at.to_dict(),
                    "reason": item.reason,
                }
                for item in value.deliveries
            ],
        }
    raise TypeError("unsupported async-work observation variant")


def decode_async_work_observation(raw: object) -> AsyncWorkObservation:
    if type(raw) is not dict or type(raw.get("kind")) is not str:
        raise ValueError("async-work observation envelope is invalid")
    kind = AsyncWorkKind(raw["kind"])
    common = {"schema", "kind", "reference", "phase", "available_actions", "detail"}
    fields = common | (
        {"result_pointer"}
        if kind is AsyncWorkKind.LOCAL_BACKGROUND_TASK
        else {"revision", "frontier", "deadline", "terminal_result", "deliveries"}
    )
    if set(raw) != fields or raw["schema"] != ASYNC_WORK_SCHEMA:
        raise ValueError("async-work observation shape is invalid")
    reference = decode_async_work_reference(raw["reference"])
    if reference.kind is not kind:
        raise ValueError("async-work observation discriminator mismatch")
    if type(raw["phase"]) is not str or type(raw["available_actions"]) is not list:
        raise ValueError("async-work observation presentation primitive is invalid")
    actions = tuple(AsyncWorkAction(item) for item in raw["available_actions"] if type(item) is str)
    if len(actions) != len(raw["available_actions"]) or len(set(actions)) != len(actions):
        raise ValueError("async-work observation actions are invalid")
    phase = AsyncWorkPresentationPhase(raw["phase"])
    detail = raw["detail"]
    if kind is AsyncWorkKind.LOCAL_BACKGROUND_TASK:
        assert isinstance(reference, LocalBackgroundTaskReference)
        if type(detail) is not dict or set(detail) != {"label", "owner_available", "pinned"}:
            raise ValueError("local observation detail shape is invalid")
        if (
            type(detail["label"]) is not str
            or type(detail["owner_available"]) is not bool
            or type(detail["pinned"]) is not bool
        ):
            raise ValueError("local observation detail primitive is invalid")
        pointer = None if raw["result_pointer"] is None else decode_task_result_pointer(raw["result_pointer"])
        return LocalBackgroundTaskObservation(
            reference,
            phase,
            LocalBackgroundObservationDetail(detail["label"], detail["owner_available"], detail["pinned"]),
            pointer,
            actions,
        )
    assert isinstance(reference, DurableWorkflowRunReference)
    if (
        type(raw["revision"]) is not int
        or raw["revision"] < 1
        or type(raw["frontier"]) is not list
        or type(raw["deliveries"]) is not list
    ):
        raise ValueError("Workflow observation collection or revision is invalid")
    if any(type(item) is not str or not item for item in raw["frontier"]):
        raise ValueError("Workflow observation frontier is invalid")
    if type(detail) is not dict or set(detail) != {"pause"}:
        raise ValueError("Workflow observation detail shape is invalid")
    pause_raw = detail["pause"]
    pause = None
    if pause_raw is not None:
        if (
            type(pause_raw) is not dict
            or set(pause_raw) != {"reason", "resume_nonce"}
            or type(pause_raw["reason"]) is not str
            or type(pause_raw["resume_nonce"]) is not str
        ):
            raise ValueError("Workflow pause detail is invalid")
        pause = WorkflowPauseDetail(WorkflowPausePresentationReason(pause_raw["reason"]), pause_raw["resume_nonce"])
    deliveries = tuple(_decode_delivery(item) for item in raw["deliveries"])
    if len({item.delivery_id for item in deliveries}) != len(deliveries):
        raise ValueError("Workflow delivery observation is duplicated")
    terminal = None if raw["terminal_result"] is None else decode_workflow_terminal_result(raw["terminal_result"])
    if terminal is not None and terminal.run_id != reference.reference.run_id:
        raise ValueError("Workflow terminal observation binds another run")
    return DurableWorkflowRunObservation(
        reference,
        raw["revision"],
        phase,
        DurableWorkflowObservationDetail(pause),
        tuple(raw["frontier"]),
        None if raw["deadline"] is None else AbsoluteInstant.from_dict(raw["deadline"]),
        terminal,
        actions,
        deliveries,
    )


def _decode_delivery(raw: object) -> WorkflowTerminalDeliveryObservation:
    fields = {"delivery_id", "destination_id", "revision", "state", "attempts", "next_eligible_at", "reason"}
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError("Workflow delivery observation shape is invalid")
    if any(type(raw[field]) is not str or not raw[field] for field in ("delivery_id", "destination_id", "state")):
        raise ValueError("Workflow delivery observation identity is invalid")
    if (
        type(raw["revision"]) is not int
        or raw["revision"] < 1
        or type(raw["attempts"]) is not int
        or raw["attempts"] < 0
    ):
        raise ValueError("Workflow delivery observation counter is invalid")
    if raw["reason"] is not None and type(raw["reason"]) is not str:
        raise ValueError("Workflow delivery observation reason is invalid")
    return WorkflowTerminalDeliveryObservation(
        raw["delivery_id"],
        raw["destination_id"],
        raw["revision"],
        WorkflowTerminalDeliveryState(raw["state"]),
        raw["attempts"],
        None if raw["next_eligible_at"] is None else AbsoluteInstant.from_dict(raw["next_eligible_at"]),
        raw["reason"],
    )


__all__ = [
    "ASYNC_WORK_SCHEMA",
    "decode_async_work_reference",
    "decode_async_work_observation",
    "encode_async_work_reference",
    "encode_async_work_observation",
]
