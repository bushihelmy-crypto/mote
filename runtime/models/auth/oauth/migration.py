"""Offline OAuth credential-v1 inventory and secret-safe v2 cutover."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import (
    CredentialMetadata,
    CredentialState,
    SecretRef,
    credential_subject,
    metadata_from_dict,
    metadata_to_dict,
)
from mote.runtime.persistence.atomic import atomic_write
from mote.runtime.secrets.cipher import AesGcmCipher, KeyFileProvider

_V1_FIELDS = {"version", "subject", "backend", "revision", "token_generation", "token"}
_V1_TOKEN_FIELDS = {"access_token", "refresh_token", "expires_at", "scopes", "claims"}


@dataclass(frozen=True, slots=True)
class OAuthMigrationInventory:
    source_digest: str
    external_name_digest: str
    backend: str
    source_revision: int
    token_generation: int


@dataclass(frozen=True, slots=True)
class OAuthMigrationCandidate:
    inventory: OAuthMigrationInventory
    metadata_path: Path
    metadata_digest: str
    secret_path: Path
    secret_digest: str


class OAuthMigrationSourceKind(StrEnum):
    SELECTOR = "selector"
    FILE = "file"
    KEYRING = "keyring"
    CONFIG = "config"
    VAULT = "vault"


class OAuthMigrationConflict(StrEnum):
    BACKEND_CONFLICT = "backend_conflict"
    CONFIG_STORE_CONFLICT = "config_store_conflict"
    MATERIAL_LOST = "material_lost"


@dataclass(frozen=True, slots=True)
class OAuthSourceEvidence:
    kind: OAuthMigrationSourceKind
    identity_digest: str
    material_digest: str | None
    backend: str | None


@dataclass(frozen=True, slots=True)
class OAuthSourceInventory:
    sources: tuple[OAuthSourceEvidence, ...]
    conflict: OAuthMigrationConflict | None


def inventory_oauth_sources(
    sources: tuple[OAuthSourceEvidence, ...],
    *,
    selected_backend: str | None,
) -> OAuthSourceInventory:
    """Resolve only secret-safe digests; raw bearer material is never retained."""

    if any(not source.identity_digest.startswith("sha256:") for source in sources):
        raise ValueError("OAuth source inventory requires stable digests")
    material = {source.material_digest for source in sources if source.material_digest is not None}
    backends = {source.backend for source in sources if source.backend is not None}
    if sources and not material:
        conflict = OAuthMigrationConflict.MATERIAL_LOST
    elif len(material) > 1 or len(backends) > 1:
        conflict = OAuthMigrationConflict.BACKEND_CONFLICT
    elif selected_backend is not None and backends and backends != {selected_backend}:
        conflict = OAuthMigrationConflict.CONFIG_STORE_CONFLICT
    else:
        conflict = None
    return OAuthSourceInventory(tuple(sorted(sources, key=lambda source: source.kind.value)), conflict)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _v1_subject(external_name: str) -> str:
    return "oauth_" + hashlib.sha256(b"mote.oauth.subject.v1\0" + external_name.encode()).hexdigest()


def _load_v1(source: Path, external_name: str) -> tuple[bytes, dict[str, object], OAuthToken]:
    data = source.read_bytes()
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy OAuth credential source is corrupt") from exc
    if type(raw) is not dict or set(raw) != _V1_FIELDS or raw.get("version") != 1:
        raise ValueError("legacy OAuth credential source is not strict v1")
    if raw["subject"] != _v1_subject(external_name) or raw["backend"] != "file":
        raise ValueError("legacy OAuth credential identity or backend conflicts")
    revision, generation, token = raw["revision"], raw["token_generation"], raw["token"]
    if type(revision) is not int or revision < 1 or type(generation) is not int or generation < 1:
        raise ValueError("legacy OAuth credential revision is invalid")
    if type(token) is not dict or set(token) != _V1_TOKEN_FIELDS:
        raise ValueError("legacy OAuth credential has no migratable token")
    try:
        decoded = OAuthToken.model_validate(token)
    except ValidationError as exc:
        raise ValueError("legacy OAuth token is invalid") from exc
    return data, raw, decoded


def inventory_v1(source: Path, external_name: str) -> OAuthMigrationInventory:
    data, raw, _ = _load_v1(source, external_name)
    revision, generation = raw["revision"], raw["token_generation"]
    assert isinstance(revision, int) and isinstance(generation, int)
    return OAuthMigrationInventory(
        _digest(data),
        _digest(external_name.encode()),
        "file",
        revision,
        generation,
    )


def build_v2_candidate(
    source: Path,
    external_name: str,
    candidate_root: Path,
    target_root: Path,
) -> OAuthMigrationCandidate:
    data, _, token = _load_v1(source, external_name)
    inventory = inventory_v1(source, external_name)
    subject = credential_subject(external_name)
    generation = inventory.token_generation
    ref = SecretRef("file", f"{subject}.{generation}.secret", generation)
    metadata = CredentialMetadata(subject, "file", inventory.source_revision, generation, CredentialState.ACTIVE, ref)
    metadata_bytes = json.dumps(metadata_to_dict(metadata), sort_keys=True, separators=(",", ":")).encode()
    cipher = AesGcmCipher(KeyFileProvider(target_root / "vault.key").key())
    secret_bytes = cipher.encrypt(token.model_dump_json().encode())
    metadata_path = candidate_root / f"{subject}.metadata.json"
    secret_path = candidate_root / "vault" / ref.key
    atomic_write(secret_path, secret_bytes, mode=0o600)
    atomic_write(metadata_path, metadata_bytes, mode=0o600)
    # Candidate read-back must pass both strict metadata and authenticated decrypt.
    decoded_metadata = metadata_from_dict(json.loads(metadata_path.read_bytes()), subject=subject, backend="file")
    if (
        OAuthToken.model_validate_json(cipher.decrypt(secret_path.read_bytes())) != token
        or decoded_metadata != metadata
    ):
        raise RuntimeError("OAuth migration candidate read-back failed")
    return OAuthMigrationCandidate(
        inventory,
        metadata_path,
        _digest(metadata_bytes),
        secret_path,
        _digest(secret_bytes),
    )


def activate_candidate(
    candidate: OAuthMigrationCandidate,
    source: Path,
    target_root: Path,
    evidence_path: Path,
    *,
    expected_source_digest: str,
    cutover_at: datetime,
) -> None:
    if cutover_at.tzinfo is None or cutover_at.utcoffset() is None:
        raise ValueError("OAuth migration cutover instant must be timezone-aware")
    if (
        candidate.inventory.source_digest != expected_source_digest
        or _digest(source.read_bytes()) != expected_source_digest
    ):
        raise ValueError("OAuth migration source changed after inventory")
    metadata = candidate.metadata_path.read_bytes()
    secret = candidate.secret_path.read_bytes()
    if _digest(metadata) != candidate.metadata_digest or _digest(secret) != candidate.secret_digest:
        raise ValueError("OAuth migration candidate changed after read-back")
    manifest = {
        "schema": "mote.oauth-migration-evidence/v1",
        "source_digest": expected_source_digest,
        "metadata_digest": candidate.metadata_digest,
        "secret_digest": candidate.secret_digest,
        "external_name_digest": candidate.inventory.external_name_digest,
        "cutover_at": cutover_at.isoformat(),
        "retire_after": (cutover_at + timedelta(days=180)).isoformat(),
    }
    atomic_write(evidence_path, json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(), mode=0o600)
    raw_metadata = json.loads(metadata)
    secret_ref = raw_metadata["secret_ref"]
    if type(secret_ref) is not dict or type(secret_ref.get("key")) is not str:
        raise ValueError("OAuth migration candidate SecretRef is invalid")
    target_secret = target_root / "vault" / secret_ref["key"]
    target_metadata = target_root / candidate.metadata_path.name
    atomic_write(target_secret, secret, mode=0o600)
    # Metadata publication is the activation point; the secret is inactive before it.
    atomic_write(target_metadata, metadata, mode=0o600)
    source.unlink()
    candidate.metadata_path.unlink()
    candidate.secret_path.unlink()
    descriptor = os.open(source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def retire_migration_evidence(
    evidence_path: Path,
    *,
    now: datetime,
    authority_id: str,
) -> str:
    if not authority_id or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("OAuth evidence retirement requires authority and absolute instant")
    try:
        raw = json.loads(evidence_path.read_bytes())
        retire_after = datetime.fromisoformat(raw["retire_after"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("OAuth migration evidence is corrupt or unsupported") from exc
    if raw.get("schema") != "mote.oauth-migration-evidence/v1" or retire_after.tzinfo is None:
        raise ValueError("OAuth migration evidence schema is unsupported")
    if now < retire_after:
        raise RuntimeError("OAuth migration evidence retention has not elapsed")
    digest = _digest(evidence_path.read_bytes())
    evidence_path.unlink()
    descriptor = os.open(evidence_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return f"{authority_id}:{digest}"


__all__ = [
    "OAuthMigrationCandidate",
    "OAuthMigrationInventory",
    "OAuthMigrationConflict",
    "OAuthMigrationSourceKind",
    "OAuthSourceEvidence",
    "OAuthSourceInventory",
    "activate_candidate",
    "build_v2_candidate",
    "inventory_v1",
    "inventory_oauth_sources",
    "retire_migration_evidence",
]
