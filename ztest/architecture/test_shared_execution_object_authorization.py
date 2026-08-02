from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _method_source(name: str) -> str:
    source = (ROOT / "product/inference/daemon/execution_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SharedEmbeddedExecutionBackend"
    )
    method = next(
        node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    return ast.get_source_segment(source, method) or ""


def test_every_object_rpc_verifies_owner_before_object_or_durable_lookup() -> None:
    for method in ("authorize", "cancel", "stream_events", "query_receipt", "reconcile"):
        source = _method_source(method)
        verify_at = source.index("_verify_owner")
        lookups = [
            position
            for token in ("_require_execution", "_receipts.get", "_session_receipts.get", "_event_store.read_events")
            if (position := source.find(token)) >= 0
        ]
        assert all(verify_at < position for position in lookups), method


def test_session_message_verifies_before_live_execution_lookup() -> None:
    session = _method_source("session")
    send = _method_source("_send_session_message")
    assert session.index("await self._send_session_message(first") < session.index("_stream_events_authorized")
    assert send.index("_verify_owner") < send.index("_require_execution")


def test_owner_record_uses_existing_sqlite_authority_and_no_parallel_store() -> None:
    backend = (ROOT / "product/inference/daemon/application.py").read_text(encoding="utf-8")
    sqlite = (ROOT / "product/inference/backends/sqlite.py").read_text(encoding="utf-8")
    assert "owners=receipts" in backend
    assert "CREATE TABLE IF NOT EXISTS execution_owner_records" in sqlite
    assert "class SQLiteExecutionOwner" not in sqlite


def test_shared_credential_preserves_authenticated_application_scope() -> None:
    credential = (ROOT / "contracts/inference/shared.py").read_text(encoding="utf-8")
    authority = (ROOT / "product/inference/daemon/security.py").read_text(encoding="utf-8")
    assert "application_id: str" in credential
    assert "application_id=handshake.application_id" in authority
