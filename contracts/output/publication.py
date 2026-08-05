"""Typed lifecycle contracts for durable final-output publication."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mote.contracts.conversation import Message


class OutputPublicationDisposition(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_ACCEPTED = "already_accepted"
    ALREADY_SETTLED = "already_settled"


@dataclass(frozen=True, slots=True)
class OutputPublicationRequest:
    publication_id: str
    source_agent_id: str
    candidate_id: str
    contract_id: str
    run_id: str
    run_kind: str
    message: Message

    def __post_init__(self) -> None:
        for field_name in (
            "publication_id",
            "source_agent_id",
            "candidate_id",
            "contract_id",
            "run_id",
            "run_kind",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"output publication {field_name} is invalid")
        if not isinstance(self.message, Message):
            raise TypeError("output publication message is invalid")


@dataclass(frozen=True, slots=True)
class OutputPublicationReceipt:
    publication_id: str
    disposition: OutputPublicationDisposition


__all__ = [
    "OutputPublicationDisposition",
    "OutputPublicationReceipt",
    "OutputPublicationRequest",
]
