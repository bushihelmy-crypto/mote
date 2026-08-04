from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from mote.runtime.models.auth.oauth.migration import (
    OAuthMigrationConflict,
    OAuthMigrationSourceKind,
    OAuthSourceEvidence,
    activate_candidate,
    build_v2_candidate,
    inventory_oauth_sources,
    inventory_v1,
    retire_migration_evidence,
)
from mote.runtime.models.auth.oauth.storage.base import CredentialUse
from mote.runtime.models.auth.oauth.storage.file_store import FileCredentialStore


def _legacy(path, external_name: str = "provider") -> None:
    subject = "oauth_" + hashlib.sha256(b"mote.oauth.subject.v1\0" + external_name.encode()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "subject": subject,
                "backend": "file",
                "revision": 2,
                "token_generation": 3,
                "token": {
                    "access_token": "secret-access",
                    "refresh_token": "secret-refresh",
                    "expires_at": 123.0,
                    "scopes": ["read"],
                    "claims": None,
                },
            }
        ),
        encoding="utf-8",
    )


def test_oauth_v1_migrates_to_encrypted_v2_and_secret_safe_evidence(tmp_path) -> None:
    source = tmp_path / "legacy.json"
    target = tmp_path / "target"
    _legacy(source)
    inventory = inventory_v1(source, "provider")
    candidate = build_v2_candidate(source, "provider", tmp_path / "candidate", target)
    evidence = tmp_path / "evidence.json"
    cutover_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    activate_candidate(
        candidate,
        source,
        target,
        evidence,
        expected_source_digest=inventory.source_digest,
        cutover_at=cutover_at,
    )

    assert not source.exists()
    assert "secret-access" not in evidence.read_text()
    store = FileCredentialStore("provider", target)
    borrowed = store.borrow(
        CredentialUse("provider", "test-account", (), "migration-test"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert borrowed is not None
    assert borrowed.token.access_token == "secret-access"
    store.release_borrow(borrowed)
    assert not candidate.metadata_path.exists() and not candidate.secret_path.exists()
    with pytest.raises(RuntimeError, match="has not elapsed"):
        retire_migration_evidence(evidence, now=cutover_at, authority_id="maintenance")
    receipt = retire_migration_evidence(
        evidence,
        now=cutover_at + timedelta(days=180),
        authority_id="maintenance",
    )
    assert receipt.startswith("maintenance:sha256:") and not evidence.exists()


def test_oauth_migration_rejects_unknown_shape_and_preimage_change(tmp_path) -> None:
    source = tmp_path / "legacy.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        inventory_v1(source, "provider")

    _legacy(source)
    inventory = inventory_v1(source, "provider")
    candidate = build_v2_candidate(source, "provider", tmp_path / "candidate", tmp_path / "target")
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        activate_candidate(
            candidate,
            source,
            tmp_path / "target",
            tmp_path / "evidence.json",
            expected_source_digest=inventory.source_digest,
            cutover_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_oauth_source_inventory_is_secret_safe_and_conflicts_fail_closed() -> None:
    inventory = inventory_oauth_sources(
        (
            OAuthSourceEvidence(OAuthMigrationSourceKind.FILE, "sha256:file", "sha256:a", "file"),
            OAuthSourceEvidence(OAuthMigrationSourceKind.KEYRING, "sha256:keyring", "sha256:b", "keyring"),
        ),
        selected_backend="file",
    )

    assert inventory.conflict is OAuthMigrationConflict.BACKEND_CONFLICT
    assert all("secret" not in evidence.identity_digest for evidence in inventory.sources)
