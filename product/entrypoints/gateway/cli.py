"""Fail-closed, structured operational commands for the inference gateway."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from mote.contracts.config.inference import DeploymentMode
from mote.contracts.inference.shared import SharedHandshake
from mote.product.config.loader import load_config
from mote.product.inference.daemon.grpc_client import SharedGrpcClient
from mote.product.inference.daemon.operations_audit import SharedOperationsAudit
from mote.product.inference.daemon.security import current_incarnation, sign_handshake
from mote.product.inference.daemon.supervisor import SharedDaemonSupervisor
from mote.product.inference.restore import IsolatedSQLiteRestoreService, RestoreApproval
from mote.product.models.runtime_generation import _shared_application_identity
from mote.product.paths import default_runtime_paths

SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_NOT_READY = 3
EXIT_CONFIG_INVALID = 4
EXIT_UNAVAILABLE = 5


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mote gateway")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--cwd", type=Path, default=None)
    parser.add_argument("--user-config-root", type=Path, default=None)
    subcommands = parser.add_subparsers(dest="action", required=True)
    subcommands.add_parser("validate", help="Validate typed gateway configuration.")
    migrate = subcommands.add_parser("migrate", help="Plan gateway schema migration.")
    migrate.add_argument("--dry-run", action="store_true", required=True)
    doctor = subcommands.add_parser("doctor", help="Probe gateway health without mutation.")
    doctor.add_argument("--timeout", type=float, default=2.0)
    status = subcommands.add_parser("upgrade-status", help="Inspect Shared generation and readiness.")
    status.add_argument("--timeout", type=float, default=2.0)
    backup = subcommands.add_parser("backup", help="Create a verified daemon-consistent backup.")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--timeout", type=float, default=30.0)
    restore = subcommands.add_parser("restore", help="Verify or apply a backup.")
    restore.add_argument("source", type=Path)
    restore_mode = restore.add_mutually_exclusive_group(required=True)
    restore_mode.add_argument("--verify-only", action="store_true")
    restore_mode.add_argument("--apply", action="store_true")
    restore.add_argument("--target-directory", type=Path)
    restore.add_argument("--approval-id")
    restore.add_argument("--approved-digest")
    restore.add_argument("--timeout", type=float, default=30.0)
    reconcile = subcommands.add_parser("reconcile", help="Reconcile incomplete durable receipts.")
    reconcile.add_argument("--timeout", type=float, default=30.0)
    drain = subcommands.add_parser("drain", help="Close admission and drain active work.")
    drain.add_argument("--timeout", type=float, default=30.0)
    return parser


def _result(command: str, status: str, code: str, **details: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": status,
        "code": code,
        "details": details,
    }


def _emit(document: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
        return
    print(f"{document['command']}: {document['status']} ({document['code']})")
    for key, value in document["details"].items():
        print(f"  {key}: {value}")


def _load(args: argparse.Namespace):
    paths = default_runtime_paths(user_config_root=args.user_config_root)
    config = load_config(
        args.cwd,
        reload=True,
        user_config_root=paths.user_config_root,
    )
    return config, paths


def _runtime_directory(config, paths) -> Path:
    shared = config.inference.shared_process
    if shared is None:
        raise ValueError("Shared Process configuration is not enabled")
    return paths.user_config_root / shared.runtime_directory


def _validate(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    try:
        config, _paths = _load(args)
    except (OSError, ValueError, ValidationError) as exc:
        return EXIT_CONFIG_INVALID, _result(
            "validate",
            "failed",
            "GATEWAY_CONFIG_INVALID",
            error_type=type(exc).__name__,
        )
    inference = config.inference
    return EXIT_OK, _result(
        "validate",
        "passed",
        "GATEWAY_CONFIG_VALID",
        deployment=inference.deployment.value,
        persistence=inference.persistence.backend.value,
        shared_process=inference.shared_process is not None,
    )


def _migrate(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    try:
        config, paths = _load(args)
    except (OSError, ValueError, ValidationError) as exc:
        return EXIT_CONFIG_INVALID, _result(
            "migrate",
            "failed",
            "GATEWAY_CONFIG_INVALID",
            error_type=type(exc).__name__,
        )
    database = paths.user_config_root / "inference" / "gateway.sqlite3"
    return EXIT_OK, _result(
        "migrate",
        "passed",
        "GATEWAY_MIGRATION_DRY_RUN",
        dry_run=True,
        deployment=config.inference.deployment.value,
        target_schema_version=config.inference.schema_version,
        database_exists=database.is_file(),
        mutations=0,
    )


async def _probe(args: argparse.Namespace, command: str) -> tuple[int, dict[str, Any]]:
    if args.timeout <= 0:
        return EXIT_CONFIG_INVALID, _result(
            command, "failed", "GATEWAY_TIMEOUT_INVALID", error="timeout must be positive"
        )
    try:
        config, paths = _load(args)
        if config.inference.deployment is not DeploymentMode.SHARED_PROCESS:
            return EXIT_OK, _result(
                command,
                "passed",
                "GATEWAY_EMBEDDED_CONFIG_VALID",
                deployment=DeploymentMode.EMBEDDED.value,
            )
        runtime_directory = _runtime_directory(config, paths)
        shared = config.inference.shared_process
        assert shared is not None
        supervisor = SharedDaemonSupervisor(runtime_directory, protocol_version=max(shared.rpc_contract_versions))
        discovery, socket_path = supervisor.discover_ready_socket()
        client = SharedGrpcClient(socket_path)
        try:
            negotiated = await client.negotiate(shared.rpc_contract_versions, capabilities=("readiness",))
            readiness = await client.get_readiness(timeout=args.timeout)
        finally:
            await client.close()
    except (OSError, RuntimeError, ValueError, ValidationError) as exc:
        return EXIT_UNAVAILABLE, _result(command, "failed", "GATEWAY_SHARED_UNAVAILABLE", error=str(exc))
    ready = bool(readiness.ready)
    status = "passed" if ready else "degraded"
    code = "GATEWAY_READY" if ready else "GATEWAY_NOT_READY"
    return (EXIT_OK if ready else EXIT_NOT_READY), _result(
        command,
        status,
        code,
        deployment=DeploymentMode.SHARED_PROCESS.value,
        socket_generation=discovery.socket_generation,
        daemon_state=discovery.state,
        protocol_version=negotiated.protocol_version,
        components=dict(readiness.components),
    )


async def _authenticated_client(args: argparse.Namespace):
    config, paths = _load(args)
    if config.inference.deployment is not DeploymentMode.SHARED_PROCESS:
        raise ValueError("operational mutation requires Shared Process deployment")
    runtime_directory = _runtime_directory(config, paths)
    shared = config.inference.shared_process
    assert shared is not None
    supervisor = SharedDaemonSupervisor(runtime_directory, protocol_version=max(shared.rpc_contract_versions))
    discovery, socket_path = supervisor.discover_ready_socket()
    client = SharedGrpcClient(socket_path)
    application_id, key_id, key = _shared_application_identity(paths.user_config_root)
    now = datetime.now(timezone.utc)
    await client.authenticate(
        sign_handshake(
            SharedHandshake(
                protocol_versions=shared.rpc_contract_versions,
                application_id=application_id,
                caller=current_incarnation(os.getpid()),
                socket_generation=discovery.socket_generation,
                tenant_id="mote-application",
                project_id="gateway-operations",
                subject_id="gateway-cli",
                policy_revision="gateway-operations-v1",
                delegation_digest="sha256:" + hashlib.sha256(b"gateway-operations-v1").hexdigest(),
                nonce=secrets.token_urlsafe(24),
                issued_at=now,
                expires_at=now + timedelta(seconds=30),
                key_id=key_id,
                signature="unsigned",
            ),
            key,
        )
    )
    return client


async def _mutate(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.timeout <= 0:
        return EXIT_CONFIG_INVALID, _result(
            args.action, "failed", "GATEWAY_TIMEOUT_INVALID", error="timeout must be positive"
        )
    client = None
    try:
        client = await _authenticated_client(args)
        if args.action == "backup":
            destination = args.destination.resolve()
            response = await client.backup(destination, consistency="crash_consistent", timeout=args.timeout)
            details = {
                "destination": response.destination,
                "consistency": response.consistency,
                "digest": response.digest,
            }
            code = "GATEWAY_BACKUP_CREATED"
        elif args.action == "restore":
            source = args.source.resolve()
            response = await client.verify_restore(source, timeout=args.timeout)
            details = {"source": str(source), "digest": response.digest}
            code = "GATEWAY_RESTORE_VERIFIED"
        elif args.action == "reconcile":
            response = await client.reconcile_all(timeout=args.timeout)
            details = {"attempts": response.attempts, "sessions": response.sessions}
            code = "GATEWAY_RECONCILIATION_COMPLETED"
        else:
            response = await client.begin_drain(timeout_seconds=args.timeout, timeout=args.timeout + 1)
            details = {"components": dict(response.components)}
            code = "GATEWAY_DRAIN_COMPLETED"
    except Exception as exc:
        return EXIT_UNAVAILABLE, _result(args.action, "failed", "GATEWAY_OPERATION_FAILED", error=str(exc))
    finally:
        if client is not None:
            await client.close()
    return EXIT_OK, _result(args.action, "passed", code, **details)


async def _restore_apply(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.timeout <= 0:
        return EXIT_CONFIG_INVALID, _result(
            "restore", "failed", "GATEWAY_TIMEOUT_INVALID", error="timeout must be positive"
        )
    if args.target_directory is None or not args.approval_id or not args.approved_digest:
        return EXIT_CONFIG_INVALID, _result("restore", "failed", "GATEWAY_RESTORE_APPROVAL_REQUIRED")
    try:
        config, paths = _load(args)
        if config.inference.deployment is not DeploymentMode.SHARED_PROCESS:
            raise ValueError("restore apply requires Shared Process deployment")
        shared = config.inference.shared_process
        if shared is None:
            raise ValueError("Shared Process configuration is required")
        runtime_directory = _runtime_directory(config, paths)
        supervisor = SharedDaemonSupervisor(
            runtime_directory,
            protocol_version=max(shared.rpc_contract_versions),
        )

        def daemon_stopped() -> bool:
            discovery = supervisor.read_discovery()
            return discovery is None or discovery.state in {
                "stopped",
                "crashed",
            }

        audit = SharedOperationsAudit(runtime_directory / "restore-operations-audit.jsonl")

        async def record(operation, outcome, details):
            await audit.record(operation, outcome, **details)

        result = await IsolatedSQLiteRestoreService(daemon_is_stopped=daemon_stopped, audit=record).apply(
            args.source.resolve(),
            args.target_directory.resolve(),
            authority_name="gateway.sqlite3",
            approval=RestoreApproval(args.approval_id, args.approved_digest),
        )
    except Exception as exc:
        return EXIT_UNAVAILABLE, _result("restore", "failed", "GATEWAY_OPERATION_FAILED", error=str(exc))
    return EXIT_OK, _result(
        "restore",
        "passed",
        "GATEWAY_RESTORE_APPLIED",
        authority=str(result.authority_path),
        digest=result.backup_digest,
        approval_id=result.approval_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "validate":
        exit_code, document = _validate(args)
    elif args.action == "migrate":
        exit_code, document = _migrate(args)
    elif args.action in {"doctor", "upgrade-status"}:
        exit_code, document = asyncio.run(_probe(args, args.action))
    elif args.action == "restore" and args.apply:
        exit_code, document = asyncio.run(_restore_apply(args))
    else:
        exit_code, document = asyncio.run(_mutate(args))
    _emit(document, as_json=args.as_json)
    return exit_code


__all__ = [
    "EXIT_CONFIG_INVALID",
    "EXIT_NOT_READY",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "main",
]
