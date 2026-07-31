"""Stable-state store generation and forward-only cutover mechanism gate."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _authorities():
    helper_spec = importlib.util.spec_from_file_location(
        "mote_governance_artifact", ROOT / "ztest/architecture/governance_artifact.py"
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
    session = helper._load("mote.runtime.session.codec", "runtime/session/codec.py")
    audit = helper._load(
        "mote.product.inference.daemon.operations_audit_codec",
        "product/inference/daemon/operations_audit_codec.py",
    )
    helper._load("mote.product.inference.backends.sqlite", "product/inference/backends/sqlite.py")
    stores = helper._load("mote.product.composition.store_governance", "product/composition/store_governance.py")
    return session, audit, stores


def _method_source(path: Path, owner_name: str, method_name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner_name)
    method = next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name
    )
    return ast.unparse(method)


def main() -> int:
    session, audit, stores = _authorities()
    violations: list[str] = []
    codecs = (*session.SESSION_ACTIVE_CODECS, audit.OPERATIONS_AUDIT_ACTIVE_CODEC)
    codec_keys = {(item.logical_store, item.event_family, item.store_generation) for item in codecs}
    declared_keys = {
        (store.logical_store, family, store.active_generation)
        for store in stores.ACTIVE_STORE_DECLARATIONS
        if store.logical_store != "inference-gateway-authority"
        for family in store.included_event_families
    }
    if codec_keys != declared_keys:
        violations.append("active store declarations do not exactly cover codec generations")
    active_units = [item.cutover_unit_id for item in stores.ACTIVE_STORE_DECLARATIONS]
    if len(active_units) != len(set(active_units)):
        violations.append("duplicate active cutover unit")
    if stores.MIGRATION_DEBT_DECLARATIONS:
        violations.append("stable online state still has migration debt")
    generations_by_store: dict[str, set[int]] = {}
    for store in stores.ACTIVE_STORE_DECLARATIONS:
        generations_by_store.setdefault(store.logical_store, set()).add(store.active_generation)
    for logical_store, generations in generations_by_store.items():
        if len(generations) != 1:
            violations.append(f"{logical_store}: multiple active generations require an explicit exit plan")
    codec_generations: dict[tuple[str, str], set[int]] = {}
    for codec in codecs:
        codec_generations.setdefault((codec.logical_store, codec.event_family), set()).add(codec.store_generation)
    for key, generations in codec_generations.items():
        if len(generations) > 1 and not stores.MIGRATION_DEBT_DECLARATIONS:
            violations.append(f"{key}: version stacking has no typed old-version exit plan")

    sqlite = _method_source(
        ROOT / "product/inference/backends/sqlite.py",
        "SQLiteAttemptReceiptStore",
        "_activate_generation",
    )
    for invariant in (
        "BEGIN IMMEDIATE",
        "GenerationState.STAGED.value",
        "GenerationState.DRAINING.value",
        "GenerationState.ACTIVE.value",
        "connection.commit()",
    ):
        if invariant not in sqlite:
            violations.append(f"persistent generation activation lacks {invariant}")
    memory = _method_source(ROOT / "runtime/inference/generation.py", "GatewayGenerationOwner", "activate")
    if "GenerationState.STAGED" not in memory or "GenerationState.DRAINING" not in memory:
        violations.append("in-memory generation owner permits fallback activation")
    history = (ROOT / "runtime/events/cutover.py").read_text(encoding="utf-8")
    if "validate_cutover_history" not in history or "cas_revision <= previous_revision" not in history:
        violations.append("cutover history does not enforce forward CAS replay")

    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("active store generations and forward-only cutover mechanisms are closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
