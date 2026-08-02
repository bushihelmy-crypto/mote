from __future__ import annotations

import ast
from pathlib import Path

from mote.runtime.models.auth.oauth.storage.base import credential_subject

ROOT = Path(__file__).resolve().parents[2]


def test_credential_subject_is_fixed_and_has_no_path_semantics() -> None:
    subjects = {credential_subject(name) for name in ("../escape", "/tmp/absolute", "a/b", "a\\b", "project:mcp")}

    assert len(subjects) == 5
    assert all(subject.startswith("oauth_") and len(subject) == 70 for subject in subjects)
    assert all("/" not in subject and "\\" not in subject and ".." not in subject for subject in subjects)


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
    source = (ROOT / "runtime/models/auth/oauth/storage/fallback_store.py").read_text(encoding="utf-8")

    assert "self._selected.load_record()" in source
    assert "self._selected.commit(" in source
    assert "except Exception" not in source[source.index("def load_record") :]
