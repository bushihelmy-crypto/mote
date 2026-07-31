"""File-domain value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Tuple, Union

from mote.contracts.content import ContentIdentity
from mote.contracts.file.identity import (
    AbsentVersion,
    FileSnapshot,
    NameIdentity,
    PathToken,
    PresentVersion,
    ProjectIdentity,
    TargetIdentity,
)


class MutationKind(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    DELETE = "delete"


class RecoveryPolicy(StrEnum):
    ROLLBACK_INCOMPLETE = "rollback_incomplete"


@dataclass(frozen=True)
class CreateMutation:
    requested_path: PathToken
    target_path: PathToken
    project_identity: ProjectIdentity
    expected_version: AbsentVersion
    after: ContentIdentity
    metadata: ContentIdentity

    kind: ClassVar[MutationKind] = MutationKind.CREATE


@dataclass(frozen=True)
class ReplaceMutation:
    before: FileSnapshot
    after: ContentIdentity

    kind: ClassVar[MutationKind] = MutationKind.REPLACE

    @property
    def requested_path(self) -> PathToken:
        return self.before.requested_path

    @property
    def target_path(self) -> PathToken:
        return self.before.target_path

    @property
    def project_identity(self) -> ProjectIdentity:
        return self.before.project_identity

    @property
    def expected_version(self) -> PresentVersion:
        return self.before.version


@dataclass(frozen=True)
class DeleteMutation:
    before: FileSnapshot

    kind: ClassVar[MutationKind] = MutationKind.DELETE

    @property
    def requested_path(self) -> PathToken:
        return self.before.requested_path

    @property
    def target_path(self) -> PathToken:
        return self.before.target_path

    @property
    def project_identity(self) -> ProjectIdentity:
        return self.before.project_identity

    @property
    def expected_version(self) -> PresentVersion:
        return self.before.version


Mutation = Union[
    CreateMutation,
    ReplaceMutation,
    DeleteMutation,
]


@dataclass(frozen=True)
class MutationSet:
    transaction_id: str
    session_id: str
    source: str
    mutations: Tuple[Mutation, ...]
    recovery_policy: RecoveryPolicy = RecoveryPolicy.ROLLBACK_INCOMPLETE

    def __post_init__(self) -> None:
        for field, value in (
            ("transaction_id", self.transaction_id),
            ("session_id", self.session_id),
            ("source", self.source),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"mutation set {field} must be a non-empty string")
        if not isinstance(self.recovery_policy, RecoveryPolicy):
            raise TypeError("mutation set recovery_policy is invalid")
        if type(self.mutations) is not tuple or not self.mutations:
            raise ValueError("mutation set must contain at least one mutation")
        for mutation in self.mutations:
            _validate_mutation_scope(mutation)
        canonical = tuple(sorted(self.mutations, key=_mutation_sort_key))
        if canonical != self.mutations:
            object.__setattr__(self, "mutations", canonical)
        names: set[NameIdentity] = set()
        targets: set[TargetIdentity] = set()
        for mutation in canonical:
            name = mutation.expected_version.name_identity
            if name in names:
                raise ValueError("mutation set contains a duplicate name identity")
            names.add(name)
            expected = mutation.expected_version
            if isinstance(expected, PresentVersion):
                if expected.target_identity in targets:
                    raise ValueError("mutation set contains a duplicate target identity")
                targets.add(expected.target_identity)


def _identity_text(identity: NameIdentity | TargetIdentity | ProjectIdentity) -> None:
    if type(identity.key) is not str or not identity.key:
        raise ValueError("file identity key must be a non-empty string")
    if type(identity.scheme) is not str or not identity.scheme:
        raise ValueError("file identity scheme must be a non-empty string")


def _validate_blob(ref: ContentIdentity) -> None:
    if not isinstance(ref, ContentIdentity):
        raise TypeError("mutation artifact reference is invalid")
    if (
        not isinstance(ref.digest, str)
        or len(ref.digest) != 64
        or any(character not in "0123456789abcdef" for character in ref.digest)
    ):
        raise ValueError("mutation artifact digest is invalid")
    if type(ref.size) is not int or not 0 <= ref.size < (1 << 63):
        raise ValueError("mutation artifact size is invalid")


def _validate_mutation_scope(mutation: Mutation) -> None:
    if not isinstance(mutation, (CreateMutation, ReplaceMutation, DeleteMutation)):
        raise TypeError("mutation set contains an unsupported mutation")
    for path in (mutation.requested_path, mutation.target_path):
        if type(path.display) is not str or not path.display:
            raise ValueError("mutation path display must be a non-empty string")
        if type(path.native) not in (str, bytes) or not path.native:
            raise ValueError("mutation native path must be non-empty text or bytes")
    _identity_text(mutation.project_identity)
    _identity_text(mutation.expected_version.name_identity)
    if isinstance(mutation.expected_version, PresentVersion):
        _identity_text(mutation.expected_version.target_identity)
    if isinstance(mutation, CreateMutation):
        if mutation.requested_path != mutation.target_path:
            raise ValueError("create mutation requested and target paths must match")
        _validate_blob(mutation.after)
        _validate_blob(mutation.metadata)
        return
    _validate_blob(mutation.before.artifact)
    _validate_blob(mutation.before.metadata)
    if isinstance(mutation, ReplaceMutation):
        _validate_blob(mutation.after)


def _mutation_sort_key(mutation: Mutation) -> tuple[str, ...]:
    expected = mutation.expected_version
    target = (
        ("", "")
        if isinstance(expected, AbsentVersion)
        else (expected.target_identity.scheme, expected.target_identity.key)
    )
    return (
        mutation.project_identity.scheme,
        mutation.project_identity.key,
        mutation.requested_path.display,
        expected.name_identity.scheme,
        expected.name_identity.key,
        target[0],
        target[1],
        mutation.kind.value,
    )
