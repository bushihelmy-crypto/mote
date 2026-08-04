from __future__ import annotations

import ast
from pathlib import Path

from mote.runtime.models.auth.oauth.storage.base import credential_subject

ROOT = Path(__file__).resolve().parents[2]


def test_credential_subject_is_fixed_and_has_no_path_semantics() -> None:
    subjects = {credential_subject(name) for name in ("../escape", "/tmp/absolute", "a/b", "a\\b", "project:mcp")}

    assert len(subjects) == 5
    values = {str(subject) for subject in subjects}
    assert all(subject.startswith("oauth_") and len(subject) == 70 for subject in values)
    assert all("/" not in subject and "\\" not in subject and ".." not in subject for subject in values)


def test_manager_never_uses_external_provider_in_durable_path() -> None:
    source = (ROOT / "runtime/models/auth/oauth/manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    joined_strings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr) and any(isinstance(value, ast.FormattedValue) for value in node.values)
    ]

    assert all("provider" not in ast.unparse(node) for node in joined_strings)
    assert "self._store.subject" in source


def test_oauth_storage_has_no_per_operation_fallback() -> None:
    assert not (ROOT / "runtime/models/auth/oauth/storage/fallback_store.py").exists()
    config = (ROOT / "contracts/config/model/oauth.py").read_text(encoding="utf-8")
    assert 'FALLBACK = "fallback"' not in config


def test_oauth_consumers_only_receive_generation_bound_borrows() -> None:
    production = tuple(path for root in (ROOT / "runtime", ROOT / "product") for path in root.rglob("*.py"))
    offenders = [path.relative_to(ROOT) for path in production if "get_valid_token" in path.read_text(encoding="utf-8")]

    assert offenders == []
    assert "acquire_valid_borrow" in (ROOT / "runtime/tools/mcp/oauth.py").read_text(encoding="utf-8")
    assert "CredentialBorrow" in (ROOT / "product/models/credential_sources.py").read_text(encoding="utf-8")


def test_keyring_is_only_a_vault_not_a_second_metadata_owner() -> None:
    source = (ROOT / "runtime/models/auth/oauth/storage/keyring_store.py").read_text(encoding="utf-8")

    assert "CredentialMetadataRepository" in source
    assert "_METADATA_SERVICE" not in source
    assert "metadata_to_dict" not in source
