from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ROOT / "zdocs" / "parity" / "operations"
CATALOG = OPERATIONS / "alert-catalog-v1.yaml"
REQUIRED_ALERT_FIELDS = {
    "id",
    "severity",
    "signal",
    "signal_plane",
    "condition",
    "for",
    "scope",
    "user_impact",
    "automatic_action",
    "runbook",
    "owner",
    "dedupe_key",
    "recovery_condition",
}
REQUIRED_COVERAGE = {
    "queue_saturation",
    "dispatch_event_loop_lag",
    "provider_breaker_open",
    "credential_quarantined",
    "usage_ledger_unavailable",
    "receipt_in_doubt_age",
    "outbox_backlog_age",
    "reconciliation_backlog",
    "artifact_publication_failed",
    "artifact_gc_blocked",
    "disk_hard_watermark",
    "sqlite_integrity_failed",
    "backup_restore_drill_failed",
    "daemon_crash_loop",
    "stale_uds_discovery",
    "connection_pool_exhausted",
    "audit_policy_failure",
    "redaction_violation",
    "plugin_isolation_failure",
    "generation_drain_stalled",
    "live_certification_stale",
}
REQUIRED_RUNBOOK_SECTIONS = {
    "diagnosis",
    "containment",
    "recovery",
    "verification",
    "escalation",
    "forbidden actions",
}


def test_alert_catalog_is_actionable_and_complete():
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    alerts = catalog["alerts"]
    assert {alert["id"] for alert in alerts} == REQUIRED_COVERAGE
    assert len(alerts) == len({alert["id"] for alert in alerts})
    for alert in alerts:
        assert set(alert) == REQUIRED_ALERT_FIELDS
        assert alert["signal_plane"] in {"caller", "daemon", "correlated"}
        assert (OPERATIONS / alert["runbook"]).is_file()


def test_operations_runbooks_have_every_required_section():
    linked = {alert["runbook"] for alert in yaml.safe_load(CATALOG.read_text(encoding="utf-8"))["alerts"]}
    for name in linked:
        headings = {
            line.removeprefix("## ").strip().lower()
            for line in (OPERATIONS / name).read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        }
        assert REQUIRED_RUNBOOK_SECTIONS <= headings, name


def test_readiness_contract_is_a_machine_decidable_component_matrix():
    readiness = (OPERATIONS / "readiness-v1.md").read_text(encoding="utf-8")
    for component in (
        "generation",
        "scheduler",
        "receipt_outbox",
        "usage_ledger",
        "credential_store",
        "artifact_store",
        "connection_pool",
        "audit_policy",
        "migration",
        "disk_capacity",
        "sqlite_integrity",
    ):
        assert f"| {component} |" in readiness
    assert "ready|degraded|failed" in readiness
    assert "Unknown required components fail closed" in readiness
