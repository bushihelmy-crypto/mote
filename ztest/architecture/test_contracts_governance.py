from __future__ import annotations

import ast
import json
from pathlib import Path

from ztest.architecture import contracts_governance as governance


def test_governance_snapshot_is_deterministic() -> None:
    first = governance.build_facts()
    second = governance.build_facts()
    assert governance._stable_facts(first) == governance._stable_facts(second)


def test_governance_facts_cover_every_contract_module() -> None:
    facts = governance.build_facts()
    expected = {path.relative_to(governance.ROOT).as_posix() for path in (governance.ROOT / "contracts").rglob("*.py")}
    actual = {module["path"] for module in facts["modules"]}
    assert actual == expected
    assert all(value == 100 for value in facts["coverage"].values())


def test_contracts_do_not_import_higher_layers() -> None:
    facts = governance.build_facts()
    illegal = {
        (module["module"], imported)
        for module in facts["modules"]
        for imported in module["imports"]
        if imported.startswith(("mote.kernel", "mote.runtime", "mote.orchestration", "mote.product"))
    }
    assert illegal == set()


def test_contracts_local_and_dynamic_import_debt_does_not_grow() -> None:
    violations: list[str] = []
    for path in sorted((governance.ROOT / "contracts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for parent in ast.walk(tree):
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(parent)):
                    violations.append(path.relative_to(governance.ROOT).as_posix())
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute):
                if isinstance(parent.func.value, ast.Name) and parent.func.value.id == "importlib":
                    violations.append(path.relative_to(governance.ROOT).as_posix())
    assert sorted(set(violations)) == []


def test_stored_facts_use_supported_envelope() -> None:
    data = json.loads(governance.FACTS.read_text(encoding="utf-8"))
    assert data["schema_version"] == governance.SCHEMA_VERSION
    assert data["canonicalization_version"] == governance.CANONICALIZATION_VERSION
    assert data["baseline_id"].startswith("sha256:")
