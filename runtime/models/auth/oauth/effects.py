"""Durable OAuth external-effect intent and settlement facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mote.runtime.ledger.append_ledger import AppendOnlyLedger, LedgerCommitReceipt
from mote.runtime.models.auth.oauth.storage.base import CredentialSubjectId

OAUTH_EFFECT_SCHEMA = "mote.oauth-effect/v1"


class OAuthEffectKind(StrEnum):
    LOGIN = "login"
    REFRESH = "refresh"
    REVOKE = "revoke"


class OAuthEffectState(StrEnum):
    INTENT_COMMITTED = "intent_committed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class OAuthEffectRecord:
    effect_id: str
    subject: CredentialSubjectId
    kind: OAuthEffectKind
    credential_revision: int
    secret_generation: int
    state: OAuthEffectState
    evidence_digest: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": OAUTH_EFFECT_SCHEMA,
                "effect_id": self.effect_id,
                "subject": str(self.subject),
                "kind": self.kind.value,
                "credential_revision": self.credential_revision,
                "secret_generation": self.secret_generation,
                "state": self.state.value,
                "evidence_digest": self.evidence_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "OAuthEffectRecord":
        fields = {
            "schema",
            "effect_id",
            "subject",
            "kind",
            "credential_revision",
            "secret_generation",
            "state",
            "evidence_digest",
        }
        if set(raw) != fields or raw.get("schema") != OAUTH_EFFECT_SCHEMA:
            raise ValueError("OAuth effect record is not strict v1")
        if (
            type(raw["effect_id"]) is not str
            or type(raw["subject"]) is not str
            or type(raw["credential_revision"]) is not int
            or raw["credential_revision"] < 0
            or type(raw["secret_generation"]) is not int
            or raw["secret_generation"] < 0
            or type(raw["evidence_digest"]) is not str
        ):
            raise ValueError("OAuth effect record primitive is invalid")
        return cls(
            raw["effect_id"],
            CredentialSubjectId(raw["subject"]),
            OAuthEffectKind(raw["kind"]),
            raw["credential_revision"],
            raw["secret_generation"],
            OAuthEffectState(raw["state"]),
            raw["evidence_digest"],
        )


class OAuthEffectStore(AppendOnlyLedger[OAuthEffectRecord]):
    def __init__(self, path: Path) -> None:
        super().__init__(path)

    @staticmethod
    def identity(subject: CredentialSubjectId, kind: OAuthEffectKind, revision: int, generation: int) -> str:
        return "oauthfx_" + hashlib.sha256(f"{subject}\0{kind.value}\0{revision}\0{generation}".encode()).hexdigest()

    def _parse_record(self, data: dict[str, object]) -> OAuthEffectRecord:
        return OAuthEffectRecord.from_dict(data)

    def _record_key(self, record: OAuthEffectRecord) -> str:
        return record.effect_id

    def _validate_transition(self, previous: OAuthEffectRecord | None, record: OAuthEffectRecord) -> None:
        if previous is None:
            if record.state is not OAuthEffectState.INTENT_COMMITTED or record.evidence_digest:
                raise ValueError("OAuth effect must begin with an empty committed intent")
            return
        if previous.state is not OAuthEffectState.INTENT_COMMITTED or record.state is OAuthEffectState.INTENT_COMMITTED:
            raise ValueError("OAuth effect lifecycle is terminal")
        if (
            previous.subject != record.subject
            or previous.kind is not record.kind
            or previous.credential_revision != record.credential_revision
            or previous.secret_generation != record.secret_generation
        ):
            raise ValueError("OAuth effect settlement forks its intent")

    def commit_intent(
        self, subject: CredentialSubjectId, kind: OAuthEffectKind, revision: int, generation: int
    ) -> OAuthEffectRecord:
        effect_id = self.identity(subject, kind, revision, generation)
        existing = self.get(effect_id)
        if existing is not None:
            return existing
        record = OAuthEffectRecord(effect_id, subject, kind, revision, generation, OAuthEffectState.INTENT_COMMITTED)
        self.append(record)
        return record

    def settle(self, effect_id: str, state: OAuthEffectState, evidence: str) -> LedgerCommitReceipt:
        if state is OAuthEffectState.INTENT_COMMITTED or not evidence:
            raise ValueError("OAuth effect settlement requires terminal evidence")
        prior = self.get(effect_id)
        if prior is None:
            raise ValueError("OAuth effect settlement has no intent")
        digest = "sha256:" + hashlib.sha256(evidence.encode()).hexdigest()
        return self.append(
            OAuthEffectRecord(
                prior.effect_id,
                prior.subject,
                prior.kind,
                prior.credential_revision,
                prior.secret_generation,
                state,
                digest,
            )
        )


__all__ = [
    "OAUTH_EFFECT_SCHEMA",
    "OAuthEffectKind",
    "OAuthEffectRecord",
    "OAuthEffectState",
    "OAuthEffectStore",
]
