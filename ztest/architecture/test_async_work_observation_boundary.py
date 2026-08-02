from pathlib import Path


def test_async_work_contract_does_not_own_ports_or_domain_projection() -> None:
    contract_sources = "\n".join(path.read_text(encoding="utf-8") for path in Path("contracts/async_work").glob("*.py"))
    assert "class LocalAsyncWorkObservationPort" not in contract_sources
    assert "class WorkflowAsyncWorkObservationPort" not in contract_sources
    assert "project_workflow_phase" not in contract_sources
    assert "project_background_task_phase" not in contract_sources
    assert "mote.orchestration" not in contract_sources


def test_workflow_identity_owner_does_not_depend_on_async_work() -> None:
    workflow_sources = "\n".join(path.read_text(encoding="utf-8") for path in Path("contracts/workflow").glob("*.py"))
    assert "contracts.async_work" not in workflow_sources


def test_product_dispatcher_has_no_domain_state_registry() -> None:
    source = Path("product/async_work/service.py").read_text(encoding="utf-8")
    for forbidden in ("BackgroundTaskPool", "WorkflowRunStore", "dict[", "registry"):
        assert forbidden not in source
