"""Architecture gate for the R2.34 Markdown Agent recovery identity."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_markdown_definition_is_content_and_source_addressed() -> None:
    loader = (ROOT / "product/agents/markdown_loader.py").read_text(encoding="utf-8")
    assert '"mote.markdown-agent-definition/v1"' in loader
    for field in (
        "canonical_path",
        "device",
        "inode",
        "content_digest",
        "approval_principal",
    ):
        assert field in loader
    assert 'definition_id = f"mote.agent.markdown.v1.sha256-' in loader
    assert "_MarkdownAgent.definition_version = definition_id" in loader
    assert "replace_role_type_registration" not in loader
    assert "**_ignored" not in loader


def test_session_residency_and_blueprint_share_definition_identity() -> None:
    role = (ROOT / "runtime/agent/role.py").read_text(encoding="utf-8")
    manager = (ROOT / "runtime/agent/session_manager.py").read_text(encoding="utf-8")
    assert "role_class=self.residency_definition_id" in role
    assert "definition_id = self.residency_definition_id" in role
    assert "return role.residency_definition_id" in manager


def test_process_global_polymorphic_restore_path_is_retired() -> None:
    base = (ROOT / "runtime/agent/base.py").read_text(encoding="utf-8")
    assert "_ROLE_REGISTRY" not in base
    assert "def load(" not in base
    assert "def __init_subclass__" not in base
