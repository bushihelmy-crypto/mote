"""File-domain value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Tuple

from mote.contracts.file.identity import AbsentVersion, FileVersion, PathToken, PresentVersion
from mote.contracts.file.mutations import DeleteMutation, MutationSet


class FileOperationKind(StrEnum):
    MUTATION = "mutation"
    REWIND = "rewind"


class TransactionStatus(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    ABORTED = "aborted"
    IN_DOUBT = "in_doubt"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTING = "rejecting"
    REJECTED = "rejected"


@dataclass(frozen=True)
class TransactionRecord:
    mutation_set: MutationSet
    status: TransactionStatus
    hunks: Tuple[HunkRecord, ...] = ()
    committed_versions: Tuple[FileVersion, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.mutation_set, MutationSet):
            raise TypeError("transaction record mutation_set is invalid")
        if not isinstance(self.status, TransactionStatus):
            raise TypeError("transaction record status is invalid")
        if self.status == TransactionStatus.COMMITTED:
            validate_committed_versions(self.mutation_set, self.committed_versions)
        elif self.committed_versions:
            raise ValueError("only a committed transaction may contain committed versions")


@dataclass(frozen=True)
class MutationResult:
    transaction_id: str
    status: TransactionStatus
    versions: Tuple[FileVersion, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class EditCommitChange:
    """One committed edit rendered from the plan's sealed B0/B1 artifacts."""

    path: PathToken
    old: str
    new: str
    post_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, PathToken):
            raise TypeError("edit commit change path is invalid")
        if type(self.old) is not str or type(self.new) is not str:
            raise TypeError("edit commit change content must be text")
        if (
            not isinstance(self.post_digest, str)
            or len(self.post_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.post_digest)
        ):
            raise ValueError("edit commit change digest is invalid")


@dataclass(frozen=True)
class EditCommitOutcome:
    """Commit result plus presentation facts derived inside File Operations."""

    result: MutationResult
    changes: Tuple[EditCommitChange, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.result, MutationResult):
            raise TypeError("edit commit outcome result is invalid")
        if type(self.changes) is not tuple or any(not isinstance(change, EditCommitChange) for change in self.changes):
            raise TypeError("edit commit outcome changes are invalid")
        if self.result.status != TransactionStatus.COMMITTED and self.changes:
            raise ValueError("only a committed edit may expose content changes")
        if self.changes and len(self.changes) != len(self.result.versions):
            raise ValueError("edit commit changes do not match committed versions")


def validate_committed_versions(
    mutation_set: MutationSet,
    versions: Tuple[FileVersion, ...],
) -> None:
    if type(versions) is not tuple or len(versions) != len(mutation_set.mutations):
        raise ValueError("committed versions must correspond one-to-one with mutations")
    for mutation, version in zip(mutation_set.mutations, versions):
        expected_name = mutation.expected_version.name_identity
        if version.name_identity != expected_name:
            raise ValueError("committed version name identity does not match mutation")
        if isinstance(mutation, DeleteMutation):
            if not isinstance(version, AbsentVersion):
                raise ValueError("delete mutation must commit an absent version")
        elif not isinstance(version, PresentVersion):
            raise ValueError("create and replace mutations must commit present versions")


@dataclass(frozen=True)
class HunkRecord:
    """Versioned durable review projection for one attributed file hunk."""

    hunk_id: str
    path: str
    session_id: str
    tool_call_id: str
    turn_index: int
    source: str
    old_range: Tuple[int, int]
    new_range: Tuple[int, int]
    pre_hash: str
    post_hash: str
    expected_digest: str
    status: ReviewStatus = ReviewStatus.PENDING
    version: int = 1
    child_transaction_id: str = ""

    @property
    def is_agent(self) -> bool:
        return self.source == "agent"

    @property
    def is_external(self) -> bool:
        return self.source == "external"
