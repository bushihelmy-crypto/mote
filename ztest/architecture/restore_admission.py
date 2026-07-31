"""Source-derived restore-capable copy inventory and admission closure."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("runtime", "orchestration", "product")
RESTORE_SOURCE_METHODS = {
    "backup_to",
    "preserve_corrupt_copy",
    "quarantine",
}


def _declared_sources():
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
    helper._load("mote.contracts.ports", "contracts/ports/__init__.py")
    helper._load("mote.contracts.ports.events", "contracts/ports/events/__init__.py")
    helper._load("mote.contracts.ports.events.journal", "contracts/ports/events/journal.py")
    helper._load("mote.contracts.events.file", "contracts/events/file/__init__.py")
    helper._load("mote.contracts.events.file.facts", "contracts/events/file/facts.py")
    helper._load("mote.runtime.session.events", "runtime/session/events.py")
    helper._load("mote.runtime.session.codec", "runtime/session/codec.py")
    helper._load(
        "mote.product.inference.daemon.operations_audit_codec",
        "product/inference/daemon/operations_audit_codec.py",
    )
    helper._load(
        "mote.product.inference.backends.sqlite",
        "product/inference/backends/sqlite.py",
    )
    stores = helper._load(
        "mote.product.composition.store_governance",
        "product/composition/store_governance.py",
    )
    return stores.RESTORE_SOURCE_CLASSIFICATIONS


def main() -> int:
    declarations = _declared_sources()
    violations: list[str] = []
    symbols = {item.source_symbol for item in declarations}
    discovered: set[str] = set()
    for root in PRODUCTION_ROOTS:
        for path in (ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module = "mote." + path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
            for owner in tree.body:
                if not isinstance(owner, ast.ClassDef):
                    continue
                for node in owner.body:
                    if (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in RESTORE_SOURCE_METHODS
                    ):
                        discovered.add(f"{module}.{owner.name}.{node.name}")
    expected = {symbol for symbol in symbols if symbol.rsplit(".", 1)[-1] in RESTORE_SOURCE_METHODS}
    if discovered != expected:
        violations.append(
            f"restore source classifier difference discovered={sorted(discovered - expected)} "
            f"stale={sorted(expected - discovered)}"
        )
    if len({item.source_id for item in declarations}) != len(declarations):
        violations.append("duplicate restore source identity")
    sqlite = (ROOT / "product/inference/backends/sqlite.py").read_text(encoding="utf-8")
    restore = (ROOT / "product/inference/restore.py").read_text(encoding="utf-8")
    for authority in (
        "INFERENCE_GATEWAY_LOGICAL_STORE",
        "INFERENCE_GATEWAY_CUTOVER_UNIT",
        "INFERENCE_GATEWAY_STORE_GENERATION",
        "INFERENCE_GATEWAY_STORAGE_FORMAT_VERSION",
    ):
        if authority not in sqlite or authority not in restore:
            violations.append(f"restore metadata does not share {authority}")
    if "target_directory must be empty" not in restore and "restore target directory must be empty" not in restore:
        violations.append("restore conversion is not isolated")
    for invariant in (
        "_BACKUP_METADATA_TABLE",
        "CREATE TABLE {_BACKUP_METADATA_TABLE}",
        "metadata.authority_digest != digest",
        "observed != metadata",
    ):
        if invariant not in sqlite:
            violations.append(f"backup metadata admission lacks {invariant}")
    if "source.stat().st_mtime" in sqlite:
        violations.append("restore creation timestamp is inferred from filesystem mtime")
    if "restore-metadata.json" in sqlite:
        violations.append("restore metadata still uses a non-atomic sidecar")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("restore-capable sources and admission metadata are closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
