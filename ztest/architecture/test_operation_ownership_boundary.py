from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_operation_owner_reuses_runtime_lease_port() -> None:
    source = (ROOT / "runtime/control/operation_ownership.py").read_text(encoding="utf-8")
    assert "LeaseCoordinator" in source
    assert "FileLeaseCoordinator" not in source
    assert "operation:{request.deployment_id}:{request.operation_id}" in source


def test_effect_guarantee_separates_state_fence_from_external_safety() -> None:
    source = (ROOT / "contracts/runtime/operation_ownership.py").read_text(encoding="utf-8")
    assert "state_mutation_fenced" in source
    assert "external_effect_replay_safe" in source
    assert "NON_REPLAYABLE" in source
    assert "IN_DOUBT" in source


def test_generic_durable_backend_and_temporal_fallback_are_retired() -> None:
    assert not (ROOT / "runtime/durable/factory.py").exists()
    assert not (ROOT / "runtime/durable/backend.py").exists()
