"""Versioned single-subject OAuth credential store contract."""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from mote.runtime.models.auth.oauth.models import OAuthToken, TokenClaims

_RECORD_VERSION = 1
_RECORD_KEYS = frozenset({"version", "subject", "backend", "revision", "token_generation", "token"})
_TOKEN_KEYS = frozenset({"access_token", "refresh_token", "expires_at", "scopes", "claims"})
_CLAIMS_KEYS = frozenset({"email", "account", "exp", "raw"})


def credential_subject(external_name: str) -> str:
    """Map an untrusted display/provider name to a fixed non-path identity."""
    if not isinstance(external_name, str) or not external_name:
        raise ValueError("OAuth credential subject source must be a non-empty string")
    digest = hashlib.sha256(b"mote.oauth.subject.v1\0" + external_name.encode("utf-8")).hexdigest()
    return f"oauth_{digest}"


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    subject: str
    backend: str
    revision: int
    token_generation: int
    token: OAuthToken | None


def record_to_dict(record: CredentialRecord) -> dict[str, object]:
    token: dict[str, object] | None = None
    if record.token is not None:
        token = record.token.model_dump(mode="json")
    return {
        "version": _RECORD_VERSION,
        "subject": record.subject,
        "backend": record.backend,
        "revision": record.revision,
        "token_generation": record.token_generation,
        "token": token,
    }


def record_from_dict(value: object, *, subject: str, backend: str) -> CredentialRecord:
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise ValueError("invalid OAuth credential record shape")
    if type(value["version"]) is not int or value["version"] != _RECORD_VERSION:
        raise ValueError("unsupported OAuth credential record version")
    if value["subject"] != subject or value["backend"] != backend:
        raise ValueError("OAuth credential record identity mismatch")
    revision = value["revision"]
    generation = value["token_generation"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("OAuth credential revision must be a positive integer")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("OAuth token generation must be a positive integer")
    raw_token = value["token"]
    token = None if raw_token is None else _decode_token(raw_token)
    return CredentialRecord(subject, backend, revision, generation, token)


def _decode_token(value: object) -> OAuthToken:
    if not isinstance(value, dict) or set(value) != _TOKEN_KEYS:
        raise ValueError("invalid OAuth token shape")
    access = value["access_token"]
    refresh = value["refresh_token"]
    expiry = value["expires_at"]
    scopes = value["scopes"]
    claims = value["claims"]
    if not isinstance(access, str) or not access:
        raise ValueError("OAuth access token must be a non-empty string")
    if refresh is not None and not isinstance(refresh, str):
        raise ValueError("OAuth refresh token must be a string or null")
    if expiry is not None and (
        isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)
    ):
        raise ValueError("OAuth expiry must be a finite number or null")
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        raise ValueError("OAuth scopes must be a list of strings")
    decoded_claims = None
    if claims is not None:
        if not isinstance(claims, dict) or set(claims) != _CLAIMS_KEYS:
            raise ValueError("invalid OAuth token claims shape")
        email, account, exp, raw = (
            claims["email"],
            claims["account"],
            claims["exp"],
            claims["raw"],
        )
        if email is not None and not isinstance(email, str):
            raise ValueError("OAuth claim email must be a string or null")
        if account is not None and not isinstance(account, str):
            raise ValueError("OAuth claim account must be a string or null")
        if exp is not None and (isinstance(exp, bool) or not isinstance(exp, int)):
            raise ValueError("OAuth claim exp must be an integer or null")
        if not isinstance(raw, dict):
            raise ValueError("OAuth raw claims must be an object")
        decoded_claims = TokenClaims(email=email, account=account, exp=exp, raw=raw)
    return OAuthToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=float(expiry) if expiry is not None else None,
        scopes=scopes,
        claims=decoded_claims,
    )


class CredentialStore(ABC):
    def __init__(self, external_name: str, *, backend: str) -> None:
        self.external_name = external_name
        self.subject = credential_subject(external_name)
        self.backend = backend

    @abstractmethod
    def load_record(self) -> CredentialRecord | None: ...

    @abstractmethod
    def commit(self, token: OAuthToken | None, *, expected_revision: int) -> CredentialRecord: ...

    def load(self) -> Optional[OAuthToken]:
        record = self.load_record()
        return record.token if record is not None else None

    def save(self, token: OAuthToken) -> None:
        current = self.load_record()
        self.commit(token, expected_revision=current.revision if current is not None else 0)

    def delete(self) -> None:
        current = self.load_record()
        if current is None or current.token is None:
            return
        self.commit(None, expected_revision=current.revision)


__all__ = [
    "CredentialRecord",
    "CredentialStore",
    "credential_subject",
    "record_from_dict",
    "record_to_dict",
]
