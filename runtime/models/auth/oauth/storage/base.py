"""Strict OAuth credential-v2 metadata and secret-vault boundary."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from mote.runtime.models.auth.oauth.models import OAuthToken

OAUTH_CREDENTIAL_SCHEMA = "mote.oauth-credential/v2"
_FIELDS = {
    "schema",
    "subject",
    "backend",
    "revision",
    "secret_generation",
    "state",
    "secret_ref",
    "legal_hold",
}
_REF_FIELDS = {"backend", "key", "generation"}


@dataclass(frozen=True, slots=True)
class CredentialSubjectId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("oauth_") or len(self.value) != 70:
            raise ValueError("OAuth credential subject identity is invalid")

    def __str__(self) -> str:
        return self.value


class CredentialState(StrEnum):
    ACTIVE = "active"
    REFRESHING = "refreshing"
    REAUTH_REQUIRED = "reauth_required"
    REVOCATION_PENDING = "revocation_pending"
    REVOKED = "revoked"
    MATERIAL_LOST = "material_lost"
    IN_DOUBT = "in_doubt"
    OWNER_ACTION_REQUIRED = "owner_action_required"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class SecretRef:
    backend: str
    key: str
    generation: int

    def __post_init__(self) -> None:
        if not self.backend or not self.key or self.generation < 1:
            raise ValueError("OAuth SecretRef is invalid")


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    subject: CredentialSubjectId
    backend: str
    revision: int
    secret_generation: int
    state: CredentialState
    secret_ref: SecretRef | None
    legal_hold: bool = False

    def __post_init__(self) -> None:
        if not self.backend or self.revision < 1 or self.secret_generation < 0:
            raise ValueError("OAuth credential metadata is invalid")
        if type(self.legal_hold) is not bool:
            raise ValueError("OAuth legal hold must be boolean")
        requires_material = self.state in {
            CredentialState.ACTIVE,
            CredentialState.REFRESHING,
            CredentialState.REVOCATION_PENDING,
        }
        forbids_material = self.state in {
            CredentialState.REAUTH_REQUIRED,
            CredentialState.REVOKED,
            CredentialState.MATERIAL_LOST,
            CredentialState.RETIRED,
        }
        if requires_material and self.secret_ref is None:
            raise ValueError("OAuth credential state and SecretRef are inconsistent")
        if forbids_material and self.secret_ref is not None:
            raise ValueError("OAuth terminal credential state cannot retain SecretRef")
        if self.secret_ref is not None and (
            self.secret_ref.backend != self.backend or self.secret_ref.generation != self.secret_generation
        ):
            raise ValueError("OAuth SecretRef does not match metadata generation")


@dataclass(frozen=True, slots=True)
class CredentialBorrow:
    borrow_id: str
    metadata: CredentialMetadata
    token: OAuthToken
    use_digest: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.borrow_id or self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("OAuth borrow identity and instants are required")
        if self.expires_at <= self.issued_at:
            raise ValueError("OAuth borrow expiry must follow issuance")


@dataclass(frozen=True, slots=True)
class CredentialUse:
    provider: str
    account: str
    scopes: tuple[str, ...]
    consumer_id: str

    def __post_init__(self) -> None:
        for value in (self.provider, self.account, self.consumer_id):
            if type(value) is not str or not value:
                raise ValueError("OAuth credential use identity is invalid")
        if not isinstance(self.scopes, tuple) or any(type(scope) is not str or not scope for scope in self.scopes):
            raise ValueError("OAuth credential use scopes are invalid")

    @property
    def digest(self) -> str:
        material = "\0".join((self.provider, self.account, *sorted(self.scopes), self.consumer_id))
        return hashlib.sha256(b"mote.oauth.use.v2\0" + material.encode()).hexdigest()


class CredentialAction(StrEnum):
    LOGOUT = "logout"
    CRYPTO_ERASE = "crypto_erase"
    RETIRE = "retire"
    APPLY_HOLD = "apply_hold"
    RELEASE_HOLD = "release_hold"
    REVALIDATE_EXPIRY = "revalidate_expiry"
    MIGRATE_BACKEND = "migrate_backend"
    RESOLVE_CONFLICT = "resolve_conflict"
    SECURITY_CLEAR = "security_clear"


class CredentialCommandDisposition(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class CredentialCommand:
    command_id: str
    subject: CredentialSubjectId
    action: CredentialAction
    authority_id: str
    expected_revision: int
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.command_id or not self.authority_id or self.expected_revision < 0:
            raise ValueError("OAuth command identity is invalid")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("OAuth command instant must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CredentialCommandReceipt:
    command_id: str
    subject: CredentialSubjectId
    action: CredentialAction
    resulting_state: CredentialState
    revision: int
    committed_at: datetime
    disposition: CredentialCommandDisposition = CredentialCommandDisposition.APPLIED
    detail: str = ""


def credential_subject(external_name: str) -> CredentialSubjectId:
    if not isinstance(external_name, str) or not external_name:
        raise ValueError("OAuth credential subject source must be a non-empty string")
    digest = hashlib.sha256(b"mote.oauth.subject.v2\0" + external_name.encode()).hexdigest()
    return CredentialSubjectId(f"oauth_{digest}")


def metadata_to_dict(record: CredentialMetadata) -> dict[str, object]:
    ref = record.secret_ref
    return {
        "schema": OAUTH_CREDENTIAL_SCHEMA,
        "subject": str(record.subject),
        "backend": record.backend,
        "revision": record.revision,
        "secret_generation": record.secret_generation,
        "state": record.state.value,
        "secret_ref": (None if ref is None else {"backend": ref.backend, "key": ref.key, "generation": ref.generation}),
        "legal_hold": record.legal_hold,
    }


def metadata_from_dict(value: object, *, subject: CredentialSubjectId, backend: str) -> CredentialMetadata:
    if type(value) is not dict or set(value) != _FIELDS or value.get("schema") != OAUTH_CREDENTIAL_SCHEMA:
        raise ValueError("OAuth credential metadata is not strict v2")
    if value["subject"] != str(subject) or value["backend"] != backend:
        raise ValueError("OAuth credential metadata identity mismatch")
    revision, generation = value["revision"], value["secret_generation"]
    if type(revision) is not int or revision < 1 or type(generation) is not int or generation < 0:
        raise ValueError("OAuth credential metadata revision is invalid")
    raw_ref = value["secret_ref"]
    ref = None
    if raw_ref is not None:
        if type(raw_ref) is not dict or set(raw_ref) != _REF_FIELDS:
            raise ValueError("OAuth credential SecretRef shape is invalid")
        if (
            type(raw_ref["backend"]) is not str
            or type(raw_ref["key"]) is not str
            or type(raw_ref["generation"]) is not int
        ):
            raise ValueError("OAuth credential SecretRef primitive is invalid")
        ref = SecretRef(raw_ref["backend"], raw_ref["key"], raw_ref["generation"])
    try:
        state = CredentialState(value["state"])
    except (TypeError, ValueError) as exc:
        raise ValueError("OAuth credential metadata state is invalid") from exc
    legal_hold = value["legal_hold"]
    if type(legal_hold) is not bool:
        raise ValueError("OAuth legal hold primitive is invalid")
    return CredentialMetadata(subject, backend, revision, generation, state, ref, legal_hold)


class CredentialStore(ABC):
    def __init__(self, external_name: str, *, backend: str) -> None:
        self.external_name = external_name
        self.subject = credential_subject(external_name)
        self.backend = backend

    @abstractmethod
    def load_metadata(self) -> CredentialMetadata | None: ...

    @abstractmethod
    def borrow(self, use: CredentialUse, *, expires_at: datetime) -> CredentialBorrow | None: ...

    @abstractmethod
    def release_borrow(self, borrow: CredentialBorrow) -> None: ...

    @abstractmethod
    def publish(self, token: OAuthToken, *, expected_revision: int) -> CredentialMetadata: ...

    @abstractmethod
    def transition(self, state: CredentialState, *, expected_revision: int) -> CredentialMetadata: ...

    @abstractmethod
    def set_legal_hold(self, enabled: bool, *, expected_revision: int) -> CredentialMetadata: ...


__all__ = [
    "CredentialBorrow",
    "CredentialMetadata",
    "CredentialState",
    "CredentialStore",
    "CredentialUse",
    "CredentialAction",
    "CredentialCommand",
    "CredentialCommandDisposition",
    "CredentialCommandReceipt",
    "CredentialSubjectId",
    "OAUTH_CREDENTIAL_SCHEMA",
    "SecretRef",
    "credential_subject",
    "metadata_from_dict",
    "metadata_to_dict",
]
