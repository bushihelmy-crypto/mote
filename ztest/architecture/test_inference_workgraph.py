from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GRAPH = ROOT / "zdocs" / "parity" / "inference-workgraph-v1.yaml"
REQUIRED = {
    "id",
    "owner_layer",
    "inputs",
    "outputs",
    "depends_on",
    "contracts_read",
    "contracts_write",
    "acceptance_tests",
    "performance_budget",
    "migration_steps",
    "rollback",
    "deletes_legacy",
    "release_scope",
    "reuses_existing",
    "existing_capability_gap",
    "infrastructure_exception_approval",
}
REQUIRED_CURRENT_PACKAGES = {
    "gate0.contracts_and_evidence",
    "reuse.existing_mote_foundations",
    "durable.receipt_outbox",
    "durable.ledger_quota_health",
    "core.scheduler_generation",
    "transport.protocol_families",
    "provider.adapters",
    "compatibility.apis_plugins",
    "shared_process.certification",
    "legacy_deletion_release",
}


def test_inference_workgraph_is_complete_acyclic_and_single_owner():
    graph = yaml.safe_load(GRAPH.read_text(encoding="utf-8"))
    packages = graph["packages"]
    by_id = {package["id"]: package for package in packages}
    assert len(by_id) == len(packages)
    outputs = {}
    contract_writers = {}
    for package in packages:
        assert REQUIRED <= package.keys()
        assert package["owner_layer"] in {
            "contracts",
            "runtime",
            "orchestration",
            "product",
        }
        assert package["release_scope"] in {"current", "future_cluster"}
        assert set(package["depends_on"]) <= by_id.keys()
        assert package["performance_budget"]["slo_revision"] == graph["slo_revision"]
        assert set(package["rollback"]) == {"preconditions", "procedure", "evidence"}
        if package["existing_capability_gap"] is not None:
            assert package["infrastructure_exception_approval"] is not None
        for output in package["outputs"]:
            assert output not in outputs, (output, outputs.get(output), package["id"])
            outputs[output] = package["id"]
        for contract in package["contracts_write"]:
            assert contract not in contract_writers
            contract_writers[contract] = package["id"]
        for test in package["acceptance_tests"]:
            assert (ROOT / test).is_file(), (package["id"], test)
        if package["release_scope"] == "current":
            assert all(by_id[dependency]["release_scope"] == "current" for dependency in package["depends_on"])

    visiting = set()
    visited = set()

    def visit(identifier):
        assert identifier not in visiting, identifier
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in by_id[identifier]["depends_on"]:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in by_id:
        visit(identifier)
    assert visited == by_id.keys()


def test_current_release_declares_every_existing_legacy_deletion_target():
    graph = yaml.safe_load(GRAPH.read_text(encoding="utf-8"))
    deletion_targets = {
        target
        for package in graph["packages"]
        if package["release_scope"] == "current"
        for target in package["deletes_legacy"]
    }
    assert deletion_targets
    assert all((ROOT / target).exists() for target in deletion_targets)


def test_workgraph_contains_the_frozen_gate_zero_minimum_dag():
    graph = yaml.safe_load(GRAPH.read_text(encoding="utf-8"))
    current = {package["id"] for package in graph["packages"] if package["release_scope"] == "current"}
    assert REQUIRED_CURRENT_PACKAGES <= current


def test_workgraph_does_not_collapse_independently_releasable_authorities():
    graph = yaml.safe_load(GRAPH.read_text(encoding="utf-8"))
    by_id = {package["id"]: package for package in graph["packages"]}
    collapsed = {
        "gateway.four_taxonomies",
        "product.routing_profiles_replay_inspector",
        "operations.backup_reconciliation",
    }
    assert collapsed.isdisjoint(by_id)
