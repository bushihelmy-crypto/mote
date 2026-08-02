from __future__ import annotations

from pathlib import Path

from mote.orchestration.agents.residency.codec import RESIDENCY_SCHEMA

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "orchestration/agents/residency/store.py"
MODEL = ROOT / "orchestration/agents/residency/model.py"


def test_residency_record_has_one_strict_versioned_owner() -> None:
    store = STORE.read_text(encoding="utf-8")
    model = MODEL.read_text(encoding="utf-8")
    assert RESIDENCY_SCHEMA == "mote.agent-residency/v1"
    for field in (
        "logical_agent_id",
        "root_agent_id",
        "parent_agent_id",
        "agent_path",
        "definition_id",
        "config_digest",
        "incarnation_generation",
        "source_session_revision",
        "record_revision",
        "materialization_fence",
    ):
        assert field in model
    assert "decode_residency_record" in store
    assert "BaseRole.load" not in store
    assert "role_loader" not in store
    assert ".get(" not in store


def test_residency_uses_trusted_factory_and_canonical_lease_session_mailbox() -> None:
    store = STORE.read_text(encoding="utf-8")
    assert "ResidentAgentFactory" in store
    assert "factory.build(record.state_snapshot)" in store
    assert "LeaseCoordinator" in store and "assert_current" in store
    assert "SessionLog" in store and "source_session_revision" in store
    assert "Mailbox.load" in store
    assert "atomic_write" in store
    assert "migrate_legacy_record" not in store
    assert "upcast_legacy" not in store
