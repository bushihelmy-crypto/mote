"""Typed acceptance facts returned when asynchronous work is submitted."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from mote.contracts.async_work.codec import decode_async_work_reference, encode_async_work_reference
from mote.contracts.async_work.identity import DurableWorkflowRunReference, LocalBackgroundTaskReference


@dataclass(frozen=True, slots=True)
class LocalBackgroundTaskSubmission:
    reference: LocalBackgroundTaskReference


@dataclass(frozen=True, slots=True)
class DurableWorkflowRunSubmission:
    reference: DurableWorkflowRunReference
    revision: int

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Workflow submission revision must be positive")


AsyncWorkSubmissionReceipt: TypeAlias = LocalBackgroundTaskSubmission | DurableWorkflowRunSubmission


def encode_async_work_submission(
    receipt: AsyncWorkSubmissionReceipt,
) -> dict[str, object]:
    if isinstance(receipt, LocalBackgroundTaskSubmission):
        return {
            "kind": "local_background_task",
            "reference": encode_async_work_reference(receipt.reference),
            "revision": None,
        }
    return {
        "kind": "durable_workflow_run",
        "reference": encode_async_work_reference(receipt.reference),
        "revision": receipt.revision,
    }


def decode_async_work_submission(raw: object) -> AsyncWorkSubmissionReceipt:
    if type(raw) is not dict or set(raw) != {"kind", "reference", "revision"}:
        raise ValueError("async-work submission shape is invalid")
    kind = raw["kind"]
    reference = decode_async_work_reference(raw["reference"])
    revision = raw["revision"]
    if kind == "local_background_task":
        if not isinstance(reference, LocalBackgroundTaskReference) or revision is not None:
            raise ValueError("local async-work submission is invalid")
        return LocalBackgroundTaskSubmission(reference)
    if kind == "durable_workflow_run":
        if not isinstance(reference, DurableWorkflowRunReference) or type(revision) is not int:
            raise ValueError("Workflow async-work submission is invalid")
        return DurableWorkflowRunSubmission(reference, revision)
    raise ValueError("async-work submission kind is unknown")


__all__ = [
    "AsyncWorkSubmissionReceipt",
    "DurableWorkflowRunSubmission",
    "LocalBackgroundTaskSubmission",
    "decode_async_work_submission",
    "encode_async_work_submission",
]
