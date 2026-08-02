from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_workflow_run_state_is_owned_only_by_orchestration() -> None:
    assert (ROOT / "orchestration/workflows/durable/store.py").is_file()
    source = (ROOT / "orchestration/workflows/durable/control.py").read_text(encoding="utf-8")
    assert "OperationOwnershipRequest" in source
    assert "expected_revision" in source
    assert "WorkflowRunStore" in source


def test_run_store_is_strict_atomic_and_fenced() -> None:
    source = (ROOT / "orchestration/workflows/durable/store.py").read_text(encoding="utf-8")
    for evidence in ("self._ownership.guard", "os.fsync", "os.replace", "set(item) != fields"):
        assert evidence in source


def test_run_record_owns_restart_activation_and_product_has_no_live_catalog() -> None:
    store = (ROOT / "orchestration/workflows/durable/store.py").read_text(encoding="utf-8")
    durability = (ROOT / "product/workflows/durability.py").read_text(encoding="utf-8")
    inspection = (ROOT / "product/workflows/inspection.py").read_text(encoding="utf-8")
    for field in (
        '"definition_source"',
        '"definition_digest"',
        '"initial_input_payload"',
        "mote.workflow-run-store/v3",
    ):
        assert field in store
    assert "_reactivate_nonterminal_runs" in durability
    assert "resolve_definition_source" in durability
    assert "self._definitions" not in durability
    assert "self._views" not in inspection


def test_definition_source_is_strict_two_variant_union() -> None:
    source = (ROOT / "contracts/workflow/definition_source.py").read_text(encoding="utf-8")
    assert 'kind == "declarative_spec"' in source
    assert 'kind == "trusted_blueprint"' in source
    assert 'raise ValueError("Workflow definition source kind is unknown")' in source
