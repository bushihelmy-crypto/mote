import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PARITY = ROOT / "zdocs" / "parity"


def test_private_network_policy_is_fail_closed():
    policy = yaml.safe_load((PARITY / "private-network-policy-v1.yaml").read_text(encoding="utf-8"))
    assert policy["default"] == "public_destinations_only"
    assert policy["private_network"]["opt_in_required"] is True
    assert set(policy["always_blocked"]) == {"link_local", "cloud_metadata_endpoints"}
    assert policy["dns"]["rebinding_revalidation"] == "required"
    assert policy["proxy"]["environment_proxy"] == "forbidden"
    assert policy["tls"]["verification"] == "mandatory"


def test_shared_sqlite_contract_binds_durability_and_conformance():
    contract = yaml.safe_load((PARITY / "shared-sqlite-semantics-v1.yaml").read_text(encoding="utf-8"))
    assert contract["authority"]["direct_caller_or_admin_access"] == "forbidden"
    assert contract["filesystem"]["local_only"] is True
    assert contract["database"]["journal_mode"] == "WAL"
    assert contract["database"]["synchronous"] == "FULL"
    assert contract["database"]["network_wait_in_transaction"] == "forbidden"
    for test in contract["conformance_tests"]:
        assert (ROOT / test).is_file()


def test_distribution_scope_keeps_cluster_out_of_current_release():
    scope = yaml.safe_load((PARITY / "deployment-distribution-scope-v1.yaml").read_text(encoding="utf-8"))
    assert set(scope["current"]["deployment_modes"]) == {"embedded", "shared_process"}
    assert set(scope["current"]["platforms"]) == {"linux_x86_64", "linux_aarch64"}
    assert scope["future_cluster"]["activation"] == "forbidden"
    assert "embedded_does_not_depend_on_grpcio" in scope["invariants"]


def test_release_cli_contract_requires_structured_safe_operations():
    contract = yaml.safe_load((PARITY / "release-cli-upgrade-v1.yaml").read_text(encoding="utf-8"))
    assert set(contract["commands"]) == {
        "validate",
        "migrate",
        "backup",
        "restore",
        "doctor",
        "reconcile",
        "drain",
        "upgrade_status",
    }
    assert all(command["stable_exit_codes"] for command in contract["commands"].values())
    assert contract["output"]["machine_json"] == "required"
    assert contract["noninteractive"]["security_approval_downgrade"] == "forbidden"
    assert contract["uninstall"]["complete_delete"].startswith("separate_dangerous_command")


def test_frozen_idl_baseline_artifacts_exist_and_match_digest():
    baseline = json.loads((PARITY / "idl-baseline-v1.json").read_text(encoding="utf-8"))
    for relative, expected in baseline["artifacts"].items():
        artifact = ROOT / relative
        assert artifact.is_file()
        assert "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest() == expected
