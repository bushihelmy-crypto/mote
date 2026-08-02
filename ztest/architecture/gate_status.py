"""CI-only gate status writer; generated results are never Runtime input."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from mote.contracts.composition.gates import (
    CheckerStatus,
    GateDeclaration,
    GateEnforcement,
    GateResult,
    GateStatusArtifact,
)

CHECKER_VERSION = "gate-status-runner-v1"
EVIDENCE_DIRECTORY = ROOT / "zdocs" / "architecture" / "gate-evidence"
STATUS_DIRECTORY = ROOT / "zdocs" / "architecture" / "gate-status"


def _build_status(
    declaration: GateDeclaration,
    *,
    source_paths: tuple[Path, ...],
    result: GateResult,
    violations: tuple[str, ...],
    evidence_path: str,
    enforcement: GateEnforcement,
    executed_at: datetime,
) -> GateStatusArtifact:
    if declaration.checker_status is not CheckerStatus.PRESENT or declaration.fixed_command is None:
        raise ValueError("only present fixed-command checkers can emit execution status")
    source = hashlib.sha256()
    for path in sorted(source_paths):
        resolved = path.resolve()
        resolved.relative_to(ROOT)
        source.update(resolved.relative_to(ROOT).as_posix().encode())
        source.update(b"\0")
        source.update(resolved.read_bytes())
        source.update(b"\0")
    declaration_bytes = json.dumps(asdict(declaration), sort_keys=True, separators=(",", ":")).encode()
    return GateStatusArtifact(
        gate_id=declaration.gate_id,
        checker_id=declaration.checker_id,
        checker_version=CHECKER_VERSION,
        executed_command=declaration.fixed_command,
        source_digest="sha256:" + source.hexdigest(),
        declaration_digest="sha256:" + hashlib.sha256(declaration_bytes).hexdigest(),
        executed_at=executed_at.astimezone(timezone.utc).isoformat(),
        checker_status=CheckerStatus.PRESENT,
        enforcement=enforcement,
        result=result,
        violations=violations,
        evidence_path=evidence_path,
        remediation_owner=declaration.declaration_owner,
    )


def render(status: GateStatusArtifact) -> str:
    payload = asdict(status)
    payload["violation_count"] = len(status.violations)
    payload["runtime_input"] = False
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def execute_gate(
    declaration: GateDeclaration,
    *,
    source_paths: tuple[Path, ...],
    enforcement: GateEnforcement,
    evidence_directory: Path = EVIDENCE_DIRECTORY,
) -> GateStatusArtifact:
    """Execute one declared command and persist its unedited process evidence."""

    if declaration.checker_status is not CheckerStatus.PRESENT or declaration.fixed_command is None:
        raise ValueError("only present fixed-command checkers can be executed")
    completed = subprocess.run(
        shlex.split(declaration.fixed_command),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    evidence_directory.mkdir(parents=True, exist_ok=True)
    evidence = evidence_directory / f"{declaration.gate_id}.txt"
    evidence.write_text(
        "command: "
        + declaration.fixed_command
        + f"\nexit_code: {completed.returncode}\n\n[stdout]\n"
        + completed.stdout
        + "\n[stderr]\n"
        + completed.stderr,
        encoding="utf-8",
    )
    try:
        evidence_path = evidence.relative_to(ROOT).as_posix()
    except ValueError:
        evidence_path = evidence.resolve().as_posix()
    violations = () if completed.returncode == 0 else (f"command exited with status {completed.returncode}",)
    return _build_status(
        declaration,
        source_paths=source_paths,
        result=GateResult.PASS if completed.returncode == 0 else GateResult.FAIL,
        violations=violations,
        evidence_path=evidence_path,
        enforcement=enforcement,
        executed_at=datetime.now(timezone.utc),
    )


def _declarations():
    """Load declaration data without executing package facades."""

    source = (ROOT / "product/composition/gates.py").read_text(encoding="utf-8")
    source = source.replace(
        "from mote.contracts.composition.gates import CheckerStatus, GateDeclaration",
        "",
        1,
    )
    namespace = {
        "CheckerStatus": CheckerStatus,
        "GateDeclaration": GateDeclaration,
    }
    exec(compile(source, "product/composition/gates.py", "exec"), namespace)
    return namespace["GATE_DECLARATIONS"]


def _source_paths() -> tuple[Path, ...]:
    roots = ("contracts", "kernel", "runtime", "orchestration", "product", "ztest/architecture")
    return tuple(path for root in roots for path in sorted((ROOT / root).rglob("*.py")))


def write_statuses(
    statuses: tuple[GateStatusArtifact, ...],
    *,
    status_directory: Path = STATUS_DIRECTORY,
) -> None:
    status_directory.mkdir(parents=True, exist_ok=True)
    existing: dict[str, GateStatusArtifact | dict[str, object]] = {}
    index_path = status_directory / "index.json"
    if index_path.is_file():
        previous = json.loads(index_path.read_text(encoding="utf-8"))
        for item in previous.get("gates", []):
            existing[str(item["gate_id"])] = item
    for status in statuses:
        (status_directory / f"{status.gate_id}.json").write_text(render(status), encoding="utf-8")
        existing[status.gate_id] = status
    gate_rows = []
    for gate_id in sorted(existing):
        item = existing[gate_id]
        if isinstance(item, GateStatusArtifact):
            result = item.result.value
            evidence_path = item.evidence_path
        else:
            result = str(item["result"])
            evidence_path = str(item["evidence_path"])
        gate_rows.append(
            {
                "gate_id": gate_id,
                "result": result,
                "status_path": f"zdocs/architecture/gate-status/{gate_id}.json",
                "evidence_path": evidence_path,
            }
        )
    index = {
        "schema": "gate-status-index-v1",
        "runtime_input": False,
        "all_passed": all(item["result"] == GateResult.PASS.value for item in gate_rows),
        "gates": gate_rows,
    }
    (status_directory / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", action="append", default=[])
    parser.add_argument(
        "--enforcement",
        choices=tuple(item.value for item in GateEnforcement),
        default=GateEnforcement.HARD.value,
    )
    arguments = parser.parse_args()
    declarations = _declarations()
    selected = (
        tuple(item for item in declarations if item.gate_id in arguments.gate) if arguments.gate else declarations
    )
    unknown = set(arguments.gate) - {item.gate_id for item in declarations}
    if unknown:
        print(f"unknown gates: {sorted(unknown)}", file=sys.stderr)
        return 2
    statuses = execute_all(
        selected,
        source_paths=_source_paths(),
        enforcement=GateEnforcement(arguments.enforcement),
    )
    write_statuses(statuses)
    return 0 if all(status.result is GateResult.PASS for status in statuses) else 1


def execute_all(
    declarations: tuple[GateDeclaration, ...],
    *,
    source_paths: tuple[Path, ...],
    enforcement: GateEnforcement,
    evidence_directory: Path = EVIDENCE_DIRECTORY,
) -> tuple[GateStatusArtifact, ...]:
    """Run gates sequentially so architecture evidence has bounded load."""

    return tuple(
        execute_gate(
            declaration,
            source_paths=source_paths,
            enforcement=enforcement,
            evidence_directory=evidence_directory,
        )
        for declaration in declarations
    )


__all__ = [
    "CHECKER_VERSION",
    "EVIDENCE_DIRECTORY",
    "execute_all",
    "execute_gate",
    "render",
    "write_statuses",
]


if __name__ == "__main__":
    raise SystemExit(main())
