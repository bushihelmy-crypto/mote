"""Isolated loader for governance authority without importing package facades."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR_VERSION = "dynamic-boundary-governance-v1"
AUTHORITY_FILES = (
    "contracts/composition/governance.py",
    "contracts/composition/gates.py",
    "contracts/events/governance.py",
    "product/composition/event_governance.py",
    "product/composition/gates.py",
    "product/composition/governance.py",
    "product/composition/store_governance.py",
)


def _package(name: str, path: Path) -> None:
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _modules():
    _package("mote", ROOT)
    _package("mote.contracts", ROOT / "contracts")
    _package("mote.contracts.composition", ROOT / "contracts/composition")
    _package("mote.contracts.events", ROOT / "contracts/events")
    _package("mote.product", ROOT / "product")
    _package("mote.product.composition", ROOT / "product/composition")
    _package("mote.product.inference", ROOT / "product/inference")
    _package("mote.product.inference.daemon", ROOT / "product/inference/daemon")
    _package("mote.runtime", ROOT / "runtime")
    _package("mote.runtime.session", ROOT / "runtime/session")
    envelope = _load("mote.contracts.events.envelope", "contracts/events/envelope.py")
    composition_governance = _load("mote.contracts.composition.governance", "contracts/composition/governance.py")
    composition_package = sys.modules["mote.contracts.composition"]
    for name in composition_governance.__all__:
        setattr(composition_package, name, getattr(composition_governance, name))
    return envelope


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def build_governance_artifact() -> dict[str, object]:
    _modules()
    _load("mote.contracts.composition.gates", "contracts/composition/gates.py")
    event_governance = _load("mote.contracts.events.governance", "contracts/events/governance.py")
    governance = _load("mote.product.composition.governance", "product/composition/governance.py")
    gates = _load("mote.product.composition.gates", "product/composition/gates.py")
    transformations = _load(
        "mote.product.composition.event_governance",
        "product/composition/event_governance.py",
    )
    _load("mote.contracts.ports", "contracts/ports/__init__.py")
    _load("mote.contracts.ports.events", "contracts/ports/events/__init__.py")
    _load("mote.contracts.ports.events.journal", "contracts/ports/events/journal.py")
    _load("mote.contracts.events.file", "contracts/events/file/__init__.py")
    _load("mote.contracts.events.file.facts", "contracts/events/file/facts.py")
    session_events = _load("mote.runtime.session.events", "runtime/session/events.py")
    session_codec = _load("mote.runtime.session.codec", "runtime/session/codec.py")
    audit_codec = _load(
        "mote.product.inference.daemon.operations_audit_codec",
        "product/inference/daemon/operations_audit_codec.py",
    )
    stores = _load(
        "mote.product.composition.store_governance",
        "product/composition/store_governance.py",
    )
    digest = hashlib.sha256()
    for relative in AUTHORITY_FILES:
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update((ROOT / relative).read_bytes())
        digest.update(b"\0")

    def declarations(items):
        return [_canonical(asdict(item)) for item in items]

    return {
        "schema": "governance-review-v1",
        "generator_version": GENERATOR_VERSION,
        "source_digest": "sha256:" + digest.hexdigest(),
        "canonicalization": "sorted-key compact JSON; declaration order is authoritative",
        "runtime_input": False,
        "classifier_version": governance.CLASSIFIER_VERSION,
        "owners": declarations(governance.OWNER_DECLARATIONS),
        "capabilities": declarations(governance.CAPABILITY_DECLARATIONS),
        "facades": declarations(governance.FACADE_DECLARATIONS),
        "candidates": declarations(governance.CANDIDATE_CLASSIFICATIONS),
        "public_symbols": declarations(governance.PUBLIC_SYMBOL_CLASSIFICATIONS),
        "wire_authorities": declarations(governance.WIRE_AUTHORITY_DECLARATIONS),
        "transformations": declarations(transformations.TRANSFORMATION_DECLARATIONS),
        "active_stores": declarations(stores.ACTIVE_STORE_DECLARATIONS),
        "archive_capabilities": declarations(stores.ARCHIVE_CAPABILITY_DECLARATIONS),
        "migration_debt": declarations(stores.MIGRATION_DEBT_DECLARATIONS),
        "restore_copies": declarations(stores.RESTORE_COPY_DECLARATIONS),
        "restore_sources": declarations(stores.RESTORE_SOURCE_CLASSIFICATIONS),
        "gates": declarations(gates.GATE_DECLARATIONS),
    }


def render() -> str:
    return json.dumps(build_governance_artifact(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    print(render(), end="")
