"""Read-only receipt queries used while settling an interrupted Act."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from mote.contracts.file import FileVersion
from mote.contracts.tool.external_effect import ToolEffectReceipt
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.contracts.tool.result import ToolPayload


class ReceiptQueryState(StrEnum):
    ABSENT = "absent"
    COMMITTED = "committed"
    ABORTED = "aborted"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class FileTransactionReceipt:
    transaction_id: str
    state: ReceiptQueryState
    versions: tuple[FileVersion, ...] = ()
    detail: str = ""


class FileTransactionReceiptQuery(Protocol):
    def query_file_transaction(self, transaction_id: str) -> FileTransactionReceipt: ...


class ExternalEffectReceiptQuery(Protocol):
    async def query_external_effect(self, identity: ToolInvocationIdentity) -> ToolEffectReceipt | None: ...


@dataclass(frozen=True, slots=True)
class ReconciledExternalEffect:
    receipt: ToolEffectReceipt
    output: str
    payload: ToolPayload | None = None

    def __post_init__(self) -> None:
        if type(self.output) is not str:
            raise TypeError("reconciled external output must be a string")
        if _presentation_digest(self.output) != self.receipt.presentation_digest:
            raise ValueError("reconciled external output does not match its receipt digest")


class ExternalEffectResultQuery(Protocol):
    async def query_external_effect_result(
        self, identity: ToolInvocationIdentity, tool_name: str
    ) -> ReconciledExternalEffect | None: ...


class InterruptedActReceiptQueries(FileTransactionReceiptQuery, ExternalEffectReceiptQuery, Protocol):
    pass


def _presentation_digest(output: str) -> str:
    return f"sha256-{hashlib.sha256(output.encode('utf-8')).hexdigest()}"


__all__ = [
    "ExternalEffectReceiptQuery",
    "ExternalEffectResultQuery",
    "FileTransactionReceipt",
    "FileTransactionReceiptQuery",
    "InterruptedActReceiptQueries",
    "ReceiptQueryState",
    "ReconciledExternalEffect",
]
