from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_reconciler_uses_durable_scan_and_operation_fence() -> None:
    source = (ROOT / "orchestration/workflows/durable/reconciliation.py").read_text(encoding="utf-8")
    for evidence in ("self._store.records()", "OperationOwnershipRequest", "self._ownership.guard", "next_eligible_at"):
        assert evidence in source


def test_external_effect_intent_receipt_and_in_doubt_are_explicit() -> None:
    source = (ROOT / "orchestration/workflows/durable/reconciliation.py").read_text(encoding="utf-8")
    for evidence in ("command_payload", "provider_receipt", "IN_DOUBT", "DEAD_LETTER"):
        assert evidence in source


def test_temporal_effect_plane_has_one_product_owner_and_no_closure_entry() -> None:
    bootstrap = (ROOT / "product/composition/bootstrap.py").read_text(encoding="utf-8")
    effects = (ROOT / "product/workflows/temporal_effects.py").read_text(encoding="utf-8")
    runtime = (ROOT / "runtime/durable/temporal/runtime.py").read_text(encoding="utf-8")
    cognition = (ROOT / "runtime/agent/components/cognition.py").read_text(encoding="utf-8")
    assert "attach_temporal_effect_plane" in bootstrap
    assert "id=effect.effect_id" in effects
    assert "async def run_step(" not in runtime
    assert "async def run_activity(" not in runtime
    assert not (ROOT / "runtime/durable/temporal/_backend.py").exists()
    assert "make_durable_backend" not in cognition
    assert not (ROOT / "runtime/durable/plugins.py").exists()


def test_temporal_command_binds_outer_identity_and_capability() -> None:
    source = (ROOT / "product/workflows/durability.py").read_text(encoding="utf-8")
    for evidence in (
        'raw["effect_id"] != command.effect_id',
        'raw["effect_id"] != command.step_id',
        'raw["capability"] != command.effect_capability.value',
    ):
        assert evidence in source
