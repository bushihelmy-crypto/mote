from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "orchestration/agents/identity/registry.py"


def test_nickname_and_path_reservation_have_one_transaction_owner() -> None:
    source = REGISTRY.read_text(encoding="utf-8")
    assert "class SpawnReservation" in source
    assert "reservation_id" in source
    assert "IdentityReservationSnapshot" in source
    assert "_active_reservations" in source
    assert "_used_agent_nicknames" not in source
    assert "random.choice" not in source
    assert ".clear()" not in source


def test_recovery_and_retention_mutations_are_fenced_and_revision_bound() -> None:
    source = REGISTRY.read_text(encoding="utf-8")
    assert "reclaim_aborted_reservation" in source
    assert "release_retained_indices" in source
    assert "LeaseCoordinator" in source
    assert "coordinator.guard" in source
    assert "claim.revision != revision" in source
    assert "claim.tombstoned" in source
    assert "AgentIndexReference" in source
    assert "resolve_index_reference" in source
    assert "_known_agent_ids" in source
