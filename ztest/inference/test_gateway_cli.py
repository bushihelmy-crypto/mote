import json
from pathlib import Path

from mote.product.entrypoints.gateway import cli


class _Persistence:
    backend = type("Backend", (), {"value": "sqlite"})()


class _Inference:
    deployment = type("Deployment", (), {"value": "embedded"})()
    persistence = _Persistence()
    shared_process = None
    schema_version = 1


class _Config:
    inference = _Inference()


def test_gateway_validate_emits_versioned_json(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_load", lambda args: (_Config(), object()))
    exit_code = cli.main(["--json", "validate"])
    document = json.loads(capsys.readouterr().out)
    assert exit_code == cli.EXIT_OK
    assert document == {
        "schema_version": 1,
        "command": "validate",
        "status": "passed",
        "code": "GATEWAY_CONFIG_VALID",
        "details": {
            "deployment": "embedded",
            "persistence": "sqlite",
            "shared_process": False,
        },
    }


def test_gateway_validate_fails_closed_without_printing_secret(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_load",
        lambda args: (_ for _ in ()).throw(ValueError("invalid storage binding")),
    )
    exit_code = cli.main(["--json", "validate"])
    output = capsys.readouterr().out
    document = json.loads(output)
    assert exit_code == cli.EXIT_CONFIG_INVALID
    assert document["status"] == "failed"
    assert document["code"] == "GATEWAY_CONFIG_INVALID"
    assert document["details"] == {"error_type": "ValueError"}
    assert "invalid storage binding" not in output


def test_gateway_doctor_reports_embedded_without_opening_shared_authority(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load", lambda args: (_Config(), object()))
    exit_code = cli.main(["--json", "doctor"])
    document = json.loads(capsys.readouterr().out)
    assert exit_code == cli.EXIT_OK
    assert document["code"] == "GATEWAY_EMBEDDED_CONFIG_VALID"
    assert document["details"]["deployment"] == "embedded"


def test_gateway_doctor_rejects_nonpositive_timeout(capsys):
    exit_code = cli.main(["--json", "doctor", "--timeout", "0"])
    document = json.loads(capsys.readouterr().out)
    assert exit_code == cli.EXIT_CONFIG_INVALID
    assert document["code"] == "GATEWAY_TIMEOUT_INVALID"


def test_gateway_restore_requires_explicit_mode():
    try:
        cli.main(["restore", "backup.sqlite3"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("restore without --verify-only was accepted")


def test_gateway_restore_apply_requires_bound_approval(capsys, tmp_path):
    exit_code = cli.main(
        [
            "--json",
            "restore",
            str(tmp_path / "backup.sqlite3"),
            "--apply",
            "--target-directory",
            str(tmp_path / "target"),
        ]
    )
    document = json.loads(capsys.readouterr().out)
    assert exit_code == cli.EXIT_CONFIG_INVALID
    assert document["code"] == "GATEWAY_RESTORE_APPROVAL_REQUIRED"


def test_gateway_mutation_rejects_embedded_deployment(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_load", lambda args: (_Config(), object()))
    exit_code = cli.main(["--json", "backup", str(tmp_path / "backup.sqlite3")])
    document = json.loads(capsys.readouterr().out)
    assert exit_code == cli.EXIT_UNAVAILABLE
    assert document["code"] == "GATEWAY_OPERATION_FAILED"


def test_gateway_migrate_is_dry_run_only_and_has_zero_mutations(monkeypatch, capsys, tmp_path):
    paths = type("Paths", (), {"user_config_root": tmp_path})()
    monkeypatch.setattr(cli, "_load", lambda args: (_Config(), paths))
    exit_code = cli.main(["--json", "migrate", "--dry-run"])
    document = json.loads(capsys.readouterr().out)
    assert exit_code == cli.EXIT_OK
    assert document["code"] == "GATEWAY_MIGRATION_DRY_RUN"
    assert document["details"]["mutations"] == 0
