"""Verify generated wire adapters against their one authoritative IDL."""

from __future__ import annotations

import filecmp
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_IMPORT = "from mote.product.inference.daemon.rpc import gateway_v1_pb2 " "as gateway__v1__pb2"


def main() -> int:
    protoc = shutil.which("protoc")
    plugin = shutil.which("grpc_python_plugin")
    if protoc is None or plugin is None:
        print("protoc and grpc_python_plugin are required", file=sys.stderr)
        return 2
    violations: list[str] = []
    declarations = _wire_declarations()
    declared_authorities = {item.authority_path for item in declarations}
    baseline_path = ROOT / "zdocs/parity/idl-baseline-v1.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_authorities = set(baseline["artifacts"])
    if declared_authorities != baseline_authorities:
        violations.append("wire authority inventory differs from the complete frozen IDL inventory")
    for declaration in declarations:
        authority = ROOT / declaration.authority_path
        if not declaration.generated_outputs:
            continue
        with tempfile.TemporaryDirectory(prefix="mote-wire-authority-") as directory:
            output = Path(directory)
            completed = subprocess.run(
                [
                    protoc,
                    "-I",
                    str(authority.parent),
                    f"--python_out={output}",
                    f"--grpc_out={output}",
                    f"--plugin=protoc-gen-grpc={plugin}",
                    str(authority),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                violations.append(f"{declaration.api_id}: protoc failed: {completed.stderr.strip()}")
                continue
            generated = {path.name: path for path in output.glob("*.py")}
            for relative in declaration.generated_outputs:
                committed = ROOT / relative
                candidate = generated.get(committed.name)
                if candidate is not None and candidate.name.endswith("_pb2_grpc.py"):
                    source = candidate.read_text(encoding="utf-8").replace(
                        "import gateway_v1_pb2 as gateway__v1__pb2",
                        _PACKAGE_IMPORT,
                        1,
                    )
                    candidate.write_text(source, encoding="utf-8")
                if candidate is None or not filecmp.cmp(committed, candidate, shallow=False):
                    violations.append(f"{declaration.api_id}: stale generated output {relative}")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("wire authority outputs are current")
    return 0


def _wire_declarations():
    """Load the authority without importing the eager package root."""

    helper_spec = importlib.util.spec_from_file_location(
        "mote_governance_artifact",
        ROOT / "ztest/architecture/governance_artifact.py",
    )
    if helper_spec is None or helper_spec.loader is None:
        raise RuntimeError("governance artifact loader is unavailable")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    helper._modules()
    helper._load("mote.contracts.events.governance", "contracts/events/governance.py")
    governance = helper._load(
        "mote.product.composition.governance",
        "product/composition/governance.py",
    )
    declarations = governance.WIRE_AUTHORITY_DECLARATIONS
    if not declarations:
        raise RuntimeError("wire authority checker requires declared APIs")
    return declarations


if __name__ == "__main__":
    raise SystemExit(main())
