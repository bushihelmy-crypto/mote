"""Strict static validator for post-closure governance manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "zdocs/post-closure-boundary-debt-implementation-requirements.md"
BASELINE = ROOT / "zdocs/architecture/post-closure-source-baseline-v1.json"
EVIDENCE = ROOT / "zdocs/architecture/post-closure-governance-evidence-v1.json"
RECEIPTS = ROOT / "zdocs/architecture/post-closure-verification-receipts-v1.json"
ALLOWED_STATES = frozenset({"OPEN", "ASSIGNED", "IN_PROGRESS", "IMPLEMENTED", "VERIFIED", "BLOCKED"})
VERIFICATION_DISPOSITIONS = frozenset({"PASS", "FAIL", "NOT_RUN"})


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path.name} must contain an object")
    return value


def _identity(value: dict[str, object]) -> str:
    body = dict(value)
    body.pop("manifest_identity", None)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _tree_digest(prefixes: tuple[str, ...]) -> str:
    entries: list[bytes] = []
    for prefix in prefixes:
        root = ROOT / prefix
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(ROOT).as_posix().encode()
            entries.append(relative + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return _digest(b"\n".join(entries) + b"\n")


def validate() -> tuple[int, int]:
    baseline = _load(BASELINE)
    evidence = _load(EVIDENCE)
    receipt_store = _load(RECEIPTS)
    assert set(baseline) == {
        "schema",
        "kind",
        "base_revision",
        "source_sets",
        "manifest_identity",
    }
    assert baseline["kind"] == "source_baseline"
    assert baseline["manifest_identity"] == _identity(baseline)
    assert set(receipt_store) == {
        "schema",
        "source_baseline_identity",
        "receipts",
        "manifest_identity",
    }
    assert receipt_store["schema"] == "post-closure-verification-receipts/v1"
    assert receipt_store["manifest_identity"] == _identity(receipt_store)
    assert receipt_store["source_baseline_identity"] == baseline["manifest_identity"]
    stored_receipts = receipt_store["receipts"]
    assert isinstance(stored_receipts, list)
    receipts_by_command: dict[str, dict[str, object]] = {}
    for receipt in stored_receipts:
        assert isinstance(receipt, dict)
        command = receipt.get("command")
        assert isinstance(command, str) and command not in receipts_by_command
        output_path = receipt.get("output_path")
        assert isinstance(output_path, str)
        path = ROOT / output_path
        assert path.is_file()
        assert receipt.get("output_digest") == _digest(path.read_bytes())
        receipts_by_command[command] = receipt
    source_sets = baseline["source_sets"]
    assert isinstance(source_sets, dict) and set(source_sets) == {
        "agents",
        "production",
        "tests",
        "requirements",
    }
    assert all(isinstance(item, str) and item.startswith("sha256:") for item in source_sets.values())
    assert source_sets == {
        "agents": _digest((ROOT / "AGENTS.md").read_bytes()),
        "production": _tree_digest(("contracts", "kernel", "runtime", "orchestration", "product")),
        "tests": _tree_digest(("ztest",)),
        "requirements": _digest(REQUIREMENTS.read_bytes()),
    }

    assert set(evidence) == {
        "schema",
        "kind",
        "source_baseline_identity",
        "requirements",
        "manifest_identity",
    }
    assert evidence["kind"] == "governance_evidence"
    assert evidence["source_baseline_identity"] == baseline["manifest_identity"]
    assert evidence["manifest_identity"] == _identity(evidence)
    records = evidence["requirements"]
    assert isinstance(records, list) and records
    ids: set[str] = set()
    dependency_edges: list[tuple[str, str]] = []
    verified = 0
    unverified_ids: set[str] = set()
    for record in records:
        assert isinstance(record, dict)
        requirement_id = record.get("requirement_id")
        assert isinstance(requirement_id, str) and requirement_id.startswith("R-")
        assert requirement_id not in ids
        ids.add(requirement_id)
        status = record.get("status")
        assert status in ALLOWED_STATES
        disposition = record.get("verification_disposition")
        assert disposition in VERIFICATION_DISPOSITIONS
        assert record.get("source_baseline_identity") == baseline["manifest_identity"]
        if status != "OPEN":
            assert record.get("execution_owner")
            assert record.get("write_set")
        dependencies = record.get("completion_dependencies")
        assert isinstance(dependencies, list) and all(isinstance(item, str) for item in dependencies)
        dependency_edges.extend((requirement_id, dependency) for dependency in dependencies)
        for path in record.get("write_set", []):
            assert isinstance(path, str) and path and (ROOT / path).exists()
        assert isinstance(record.get("decision_ids"), list)
        assert record.get("decision_ids")
        assert isinstance(record.get("recipe_ids"), list)
        assert record.get("migration_disposition")
        assert record.get("recovery_conditions")
        receipts = record.get("verification_receipts")
        assert isinstance(receipts, list)
        assert isinstance(record.get("legacy_exit_receipts"), list)
        retired_paths = record.get("retired_paths")
        assert isinstance(retired_paths, list)
        assert all(isinstance(path, str) and not (ROOT / path).exists() for path in retired_paths)
        if status == "VERIFIED":
            verified += 1
            assert disposition == "PASS"
            assert record.get("evidence") and record.get("approval_authority")
            assert record.get("verification_instant") and record.get("activation_generation")
            assert record.get("integrated_source_identity") == baseline["manifest_identity"]
            commands = record.get("verification_commands")
            assert isinstance(commands, list) and len(receipts) == len(commands) >= 1
            assert {receipt.get("command") for receipt in receipts} == set(commands)
            for receipt in receipts:
                assert isinstance(receipt, dict)
                assert receipt.get("source_baseline_identity") == baseline["manifest_identity"]
                assert receipt.get("completed_source_baseline_identity") == baseline["manifest_identity"]
                assert receipt.get("exit_code") == 0
                assert isinstance(receipt.get("output_digest"), str)
                assert isinstance(receipt.get("started_at"), str)
                assert isinstance(receipt.get("finished_at"), str)
                assert receipts_by_command.get(str(receipt.get("command"))) == receipt
                environment = receipt.get("environment")
                assert isinstance(environment, dict) and environment.get("pytest_parallel") is False
            for item in record["evidence"]:
                assert isinstance(item, dict) and set(item) == {"path", "digest"}
                path = ROOT / item["path"]
                assert path.is_file()
                assert item["digest"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            unverified_ids.add(requirement_id)
        if disposition == "FAIL":
            assert status != "VERIFIED"
            assert record.get("failure_reasons")
            assert record.get("recovery_conditions")
    assert "R-W0-GOVERNANCE-001" in ids
    assert unverified_ids <= {"R-W0-GOVERNANCE-001"}
    assert all(dependency in ids for _, dependency in dependency_edges)
    return len(ids), verified
