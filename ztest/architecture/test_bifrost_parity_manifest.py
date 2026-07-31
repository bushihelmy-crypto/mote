import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "zdocs" / "parity" / "bifrost-ec1dd920.yaml"
CERTIFICATION = ROOT / "zdocs" / "parity" / "provider-certification-v1.yaml"
EXECUTION_CONTRACTS = ROOT / "zdocs" / "parity" / "execution-contracts-v1.yaml"
FAILURE_CONTRACT = ROOT / "zdocs" / "parity" / "canonical-failure-v2.yaml"
RPC_CONTRACT = ROOT / "zdocs" / "parity" / "rpc" / "gateway-v1.proto"
DEPENDENCY_PLAN = ROOT / "zdocs" / "parity" / "dependency-plan-v1.yaml"
DEPENDENCY_LOCK = ROOT / "requirements" / "inference.lock"
DEPENDENCY_SBOM = ROOT / "zdocs" / "parity" / "inference-sbom-v1.cdx.json"
DEPENDENCY_REVIEW = ROOT / "zdocs" / "parity" / "dependency-review-v1.yaml"
DEPENDENCY_PLATFORMS = ROOT / "zdocs" / "parity" / "dependency-platform-matrix-v1.yaml"
INFERENCE_OPENAPI = ROOT / "zdocs" / "parity" / "api" / "inference-v1.openapi.yaml"
ADMIN_OPENAPI = ROOT / "zdocs" / "parity" / "api" / "admin-v1.openapi.yaml"
ASYNCAPI = ROOT / "zdocs" / "parity" / "api" / "realtime-webhook-v1.asyncapi.yaml"
RBAC = ROOT / "zdocs" / "parity" / "admin-rbac-v1.yaml"
TRANSLATION = ROOT / "zdocs" / "parity" / "translation-profiles-v1.yaml"
REASONING_REPLAY = ROOT / "zdocs" / "parity" / "reasoning-replay-v1.yaml"
ROUTING_DECISION = ROOT / "zdocs" / "parity" / "routing-decision-v1.yaml"
RESPONSE_VALIDATOR = ROOT / "zdocs" / "parity" / "response-validator-v1.yaml"
TRAFFIC_INSPECTOR = ROOT / "zdocs" / "parity" / "traffic-inspector-v1.yaml"
REUSE_AUDIT = ROOT / "zdocs" / "parity" / "infrastructure-reuse-audit-v1.yaml"
RECOVERY = ROOT / "zdocs" / "parity" / "recovery-contracts-v1.yaml"
OPERATIONS = ROOT / "zdocs" / "parity" / "observability-operations-v1.yaml"
FAULT_MATRIX = ROOT / "zdocs" / "parity" / "fault-matrix-v1.yaml"
QUALITY_RATCHET = ROOT / "zdocs" / "parity" / "quality-ratchet-v1.yaml"
QUALITY_RESULT = ROOT / "zdocs" / "parity" / "quality-ratchet-result-v1.json"
WIRE_FIXTURES = ROOT / "zdocs" / "parity" / "wire-fixtures-v1.yaml"
SLO = ROOT / "zdocs" / "parity" / "inference-slo-v1.yaml"
APPROVAL = ROOT / "zdocs" / "parity" / "GATE_0_APPROVED.yaml"
EXPECTED_COMMIT = "ec1dd920619955415bd6d61ab9ecff71f170ee22"
ALLOWED_STATUS = {"supported", "unsupported", "conditional", "provider_managed"}
ALLOWED_SCOPE = {"current_embedded", "current_shared", "future_cluster"}
ALLOWED_TAXONOMY = {"unary_finite_attempt", "durable_operation", "long_lived_session", "artifact_transfer"}


def _manifest():
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_frozen_baseline_and_cartesian_coverage():
    manifest = _manifest()
    assert manifest["baseline"]["commit"] == EXPECTED_COMMIT
    assert manifest["baseline"]["dirty"] is False
    providers = manifest["providers"]
    operations = manifest["operations"]
    cells = manifest["cells"]
    assert len(providers) == 29
    assert len(cells) == len(providers) * len(operations)
    assert {(cell["provider"], cell["operation"]) for cell in cells} == {
        (provider, operation) for provider in providers for operation in operations
    }


def test_every_cell_has_gate_zero_lifecycle_and_evidence():
    for cell in _manifest()["cells"]:
        assert cell["status"] in ALLOWED_STATUS
        assert cell["release_scope"] and set(cell["release_scope"]) <= ALLOWED_SCOPE
        assert cell["evidence"]["source"]
        assert cell["evidence"]["source_digest"].startswith("sha256:")
        assert cell["mote_contract_tests"]
        assert cell["execution"]["taxonomy"] in ALLOWED_TAXONOMY
        for key in (
            "logical_owner",
            "wire_unit",
            "commit_boundary",
            "fallback_boundary",
            "poll_owner",
            "reconcile",
            "generation_pin",
            "usage_settlement",
            "terminal_or_in_doubt_oracle",
        ):
            assert cell["execution"][key]
        assert cell["translation"]["profile"]
        assert cell["translation"]["round_trip"]
        assert cell["catalog"]["provenance"]
        assert cell["reasoning_replay"] in {"required", "forbidden", "not_applicable"}


def test_certification_inventory_covers_every_current_supported_cell():
    manifest = _manifest()
    certification = yaml.safe_load(CERTIFICATION.read_text(encoding="utf-8"))
    expected = {
        f'{cell["provider"]}.{cell["operation"]}'
        for cell in manifest["cells"]
        if cell["status"] in {"supported", "conditional", "provider_managed"}
        and {"current_embedded", "current_shared"} & set(cell["release_scope"])
    }
    resources = certification["resources"]
    assert {resource["cell_id"] for resource in resources} == expected
    assert len(resources) == len(expected)
    for resource in resources:
        assert resource["credential_class"]
        assert resource["resource_owner"]
        assert resource["required_suites"] == ["offline_protocol", "recorded_contract", "live_certification"]
        assert resource["cleanup"]["procedure"]
        assert resource["fixture_freshness"]["max_age_days"] > 0


def test_execution_taxonomies_have_one_owner_and_runtime_port():
    contract = yaml.safe_load(EXECUTION_CONTRACTS.read_text(encoding="utf-8"))
    assert set(contract["taxonomies"]) == ALLOWED_TAXONOMY
    owners = []
    for taxonomy in contract["taxonomies"].values():
        assert taxonomy["logical_owner"]
        assert taxonomy["runtime_port"]
        assert taxonomy["journal"]
        assert taxonomy["receipt"]
        owners.append(taxonomy["logical_owner"])
    assert len(owners) == len(set(owners))


def test_canonical_failure_contract_has_all_authority_verdicts():
    contract = yaml.safe_load(FAILURE_CONTRACT.read_text(encoding="utf-8"))
    assert contract["schema_version"] == 2
    assert set(contract["fields"]) == {
        "domain",
        "reason",
        "retryability",
        "health_verdict",
        "external_commit_state",
        "credential_verdict",
        "quota_observation",
        "provider_code",
        "safe_message",
        "reconcile_strategy",
        "usage_observation",
        "http_compatibility_class",
    }
    owned_fields = {field for fields in contract["consumer_ownership"].values() for field in fields}
    assert owned_fields <= set(contract["fields"])


def test_shared_rpc_contract_covers_all_execution_taxonomies_and_recovery():
    proto = RPC_CONTRACT.read_text(encoding="utf-8")
    for method in (
        "StartUnary",
        "StartDurableCommand",
        "OpenSession",
        "ExecuteTransferPart",
        "AuthorizeWire",
        "Cancel",
        "StreamEvents",
        "Session",
        "QueryReceipt",
        "Reconcile",
        "Negotiate",
        "StageGeneration",
        "GetReadiness",
    ):
        assert f"rpc {method}(" in proto
    for binding in (
        "generation_artifact_digest",
        "principal_proof",
        "deadline_utc",
        "remaining_seconds_at_send",
        "idempotency_key",
        "receipt_revision",
    ):
        assert binding in proto


def test_dependency_plan_has_one_owner_and_supply_chain_policy():
    plan = yaml.safe_load(DEPENDENCY_PLAN.read_text(encoding="utf-8"))
    components = plan["components"]
    assert DEPENDENCY_LOCK.is_file()
    assert DEPENDENCY_SBOM.is_file()
    assert DEPENDENCY_REVIEW.is_file()
    assert DEPENDENCY_PLATFORMS.is_file()
    owners = [component["owner"] for component in components]
    assert len(owners) == len(set(owners))
    for component in components:
        assert component["version_range"]
        assert component["license"]
        assert component["import_layer"] == "product"
        assert component["sbom_component"].startswith("pkg:pypi/")
        assert component["platforms"]
        assert component["release_scope"]
        assert component["cve_policy"]["remediation_sla_days"] > 0
        if component["optional"]:
            assert component["extra"]


def test_external_api_contracts_and_admin_scopes_are_frozen():
    inference = yaml.safe_load(INFERENCE_OPENAPI.read_text(encoding="utf-8"))
    admin = yaml.safe_load(ADMIN_OPENAPI.read_text(encoding="utf-8"))
    asyncapi = yaml.safe_load(ASYNCAPI.read_text(encoding="utf-8"))
    rbac = yaml.safe_load(RBAC.read_text(encoding="utf-8"))
    assert inference["openapi"] == "3.1.0"
    assert "/v1/chat/completions" in inference["paths"]
    assert "/v1/responses" in inference["paths"]
    assert asyncapi["asyncapi"] == "3.0.0"
    assert set(asyncapi["channels"]) == {"realtime", "webhook"}
    allowed_scopes = {scope for scopes in rbac["roles"].values() for scope in scopes}
    for path in admin["paths"].values():
        for operation in path.values():
            if isinstance(operation, dict) and "operationId" in operation:
                assert operation["x-scope"] in allowed_scopes or "*" in allowed_scopes
    assert rbac["constraints"]["credential_secret_read_scope_exists"] is False


def test_translation_replay_routing_validator_and_inspector_do_not_create_hidden_authorities():
    translation = yaml.safe_load(TRANSLATION.read_text(encoding="utf-8"))
    replay = yaml.safe_load(REASONING_REPLAY.read_text(encoding="utf-8"))
    routing = yaml.safe_load(ROUTING_DECISION.read_text(encoding="utf-8"))
    validator = yaml.safe_load(RESPONSE_VALIDATOR.read_text(encoding="utf-8"))
    inspector = yaml.safe_load(TRAFFIC_INSPECTOR.read_text(encoding="utf-8"))
    assert set(translation["profiles"]) == {"openai_family_v1", "anthropic_messages_v1", "google_generate_content_v1"}
    assert "no_private_reasoning_database" in replay["invariants"]
    assert routing["dry_run"]["wire_requests"] == 0
    assert routing["dry_run"]["mutations"] == 0
    assert validator["invariants"][0] == "http_2xx_is_not_success"
    assert inspector["mode"] == "read_only_projection"
    assert "replay_wire_request" in inspector["forbidden_capabilities"]


def test_infrastructure_reuse_audit_forbids_duplicate_foundations():
    audit = yaml.safe_load(REUSE_AUDIT.read_text(encoding="utf-8"))
    assert audit["exceptions"] == []
    for finding in audit["findings"].values():
        assert finding["decision"]
        assert finding["implementations"]
        for key, value in finding.items():
            if key.endswith("_approved"):
                assert value is False


def test_backup_reconciliation_observability_and_operations_contracts_are_closed():
    recovery = yaml.safe_load(RECOVERY.read_text(encoding="utf-8"))
    operations = yaml.safe_load(OPERATIONS.read_text(encoding="utf-8"))
    assert recovery["barrier"]["stale_epoch_first_consumption"] == "reject"
    assert recovery["reconciliation"]["no_evidence_terminal"] == "forbidden"
    assert recovery["reconciliation"]["logical_owner_authority"] == [
        "accept_or_reject_proposal",
        "append_terminal",
        "settle_usage",
    ]
    assert set(operations["planes"]) == {"caller", "daemon"}
    assert len(operations["alerts"]) >= 6
    for alert in operations["alerts"]:
        assert (ROOT / "zdocs" / "parity" / "runbooks" / f'{alert["runbook"]}.md').is_file()
    assert operations["cli"]["destructive_actions_require_approval"] is True


def test_fault_matrix_and_quality_ratchets_never_treat_coverage_as_oracle():
    faults = yaml.safe_load(FAULT_MATRIX.read_text(encoding="utf-8"))
    quality = yaml.safe_load(QUALITY_RATCHET.read_text(encoding="utf-8"))
    assert "zero_or_one" in faults["oracle"]
    assert len(faults["cases"]) >= 25
    assert all(case["status"] in {"passed", "pending"} for case in faults["cases"])
    assert quality["coverage_policy"] == "auxiliary_only"
    assert QUALITY_RESULT.is_file()
    result = json.loads(QUALITY_RESULT.read_text(encoding="utf-8"))
    assert set(result["results"]) == set(quality["ratchets"])
    assert (quality["gate_status"] == "passed") is (result["gate_status"] == "passed")
    assert all(ratchet.get("blocking", ratchet.get("blocking_when_frozen")) for ratchet in quality["ratchets"].values())


def test_gate_zero_readiness_is_fail_closed_until_every_external_evidence_is_ready():
    certification = yaml.safe_load(CERTIFICATION.read_text(encoding="utf-8"))
    dependency = yaml.safe_load(DEPENDENCY_PLAN.read_text(encoding="utf-8"))
    fixtures = yaml.safe_load(WIRE_FIXTURES.read_text(encoding="utf-8"))
    quality = yaml.safe_load(QUALITY_RATCHET.read_text(encoding="utf-8"))
    slo_frozen = False
    if SLO.is_file():
        slo = yaml.safe_load(SLO.read_text(encoding="utf-8"))
        slo_frozen = slo["gate_status"] == "frozen"
    ready = (
        certification["gate_status"] == "passed"
        and dependency["gate_status"] == "passed"
        and fixtures["gate_status"] == "passed"
        and quality["gate_status"] == "passed"
        and slo_frozen
    )
    assert APPROVAL.exists() is ready
