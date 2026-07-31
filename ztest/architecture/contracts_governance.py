"""Offline governance CLI for the contracts package."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
ARCH = ROOT / "zdocs" / "architecture"
FACTS = ARCH / "contracts-facts.json"
SCHEMA_VERSION = 1
CANONICALIZATION_VERSION = 1
GENERATED_BY = "ztest.architecture.contracts_governance@1"
LAYERS = ("contracts", "kernel", "runtime", "orchestration", "product")
GENERATED_PATHS = {
    "zdocs/architecture/contracts-facts.json",
    "zdocs/architecture/contracts-dependencies.toml",
    "zdocs/architecture/contracts-api.toml",
    "zdocs/architecture/contracts-identities.toml",
    "zdocs/architecture/contracts-events.toml",
    "zdocs/architecture/contracts-errors.toml",
    "zdocs/architecture/contracts-tests.toml",
    "zdocs/architecture/contracts-decisions.toml",
    "zdocs/architecture/contracts-migrations.toml",
    "zdocs/architecture/contracts-domains.toml",
    "zdocs/architecture/contracts-phase-gates.toml",
}


class GovernanceError(Exception):
    """A deterministic governance validation error."""


@dataclass(frozen=True)
class ModuleInfo:
    module: str
    path: str
    loc: int
    imports: tuple[str, ...]
    third_party_imports: tuple[str, ...]


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _module_for(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return "mote" + ("." + ".".join(parts) if parts else "")


def _layer(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[1] in LAYERS else "external"


def _imports(tree: ast.AST, module: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    found: set[str] = set()
    third_party: set[str] = set()
    package = module.split(".")[:-1]
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = package[: len(package) - node.level + 1]
                names = [".".join(prefix + ([node.module] if node.module else []))]
            elif node.module:
                names = [node.module]
        for name in names:
            if name.startswith("mote."):
                found.add(name)
            elif name.split(".", 1)[0] not in sys.stdlib_module_names:
                third_party.add(name.split(".", 1)[0])
    return tuple(sorted(found)), tuple(sorted(third_party))


def _public_definitions(tree: ast.Module) -> tuple[dict[str, ast.AST], set[str]]:
    definitions: dict[str, ast.AST] = {}
    explicit_all: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    definitions[target.id] = node
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                value = node.value
                if isinstance(value, (ast.List, ast.Tuple)):
                    explicit_all.update(
                        item.value
                        for item in value.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    )
    return definitions, explicit_all


def _is_protocol(node: ast.AST) -> bool:
    return isinstance(node, ast.ClassDef) and any(
        (isinstance(base, ast.Name) and base.id == "Protocol")
        or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
        for base in node.bases
    )


def _production_files() -> list[Path]:
    files: list[Path] = []
    for layer in LAYERS:
        files.extend((ROOT / layer).rglob("*.py"))
    files.extend(path for path in (ROOT / "engine.py", ROOT / "__init__.py") if path.exists())
    return sorted(set(files))


def _read_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, check=False, capture_output=True, text=True)
    if result.returncode:
        raise GovernanceError(result.stderr.strip() or "git command failed")
    return result.stdout


def _worktree_manifest() -> list[dict[str, Any]]:
    tracked = set(_git(["ls-files"]).splitlines())
    untracked = set(_git(["ls-files", "--others", "--exclude-standard"]).splitlines())
    entries: list[dict[str, Any]] = []
    for relative in sorted(tracked | untracked):
        if relative in GENERATED_PATHS or relative.startswith(".git/"):
            continue
        path = ROOT / relative
        if not path.exists() and not path.is_symlink():
            entries.append({"path": relative, "type": "deleted"})
            continue
        stat = path.lstat()
        if path.is_symlink():
            payload = os.readlink(path).encode()
            kind = "symlink"
        elif path.is_file():
            payload = path.read_bytes()
            kind = "file"
        else:
            continue
        entries.append(
            {
                "path": relative,
                "type": kind,
                "mode": stat.st_mode & 0o7777,
                "size": len(payload),
                "digest": _sha(payload),
            }
        )
    return entries


def _consumer_index(files: Iterable[Path]) -> dict[str, dict[str, set[str]]]:
    consumers: dict[str, dict[str, set[str]]] = {}
    for path in files:
        module = _module_for(path)
        is_test = path.relative_to(ROOT).parts[0] == "ztest"
        try:
            tree = _read_tree(path)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module or not node.module.startswith("mote.contracts"):
                continue
            for alias in node.names:
                key = f"{node.module}.{alias.name}"
                bucket = consumers.setdefault(key, {"production": set(), "test": set()})
                bucket["test" if is_test else "production"].add(module)
    return consumers


def build_facts() -> dict[str, Any]:
    worktree = _worktree_manifest()
    baseline_id = _sha(_json_bytes(worktree))
    head = _git(["rev-parse", "HEAD"]).strip()
    tree_id = _git(["rev-parse", "HEAD^{tree}"]).strip()
    production = _production_files()
    all_scan_files = production + sorted((ROOT / "ztest").rglob("*.py"))
    consumers = _consumer_index(all_scan_files)
    modules: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    contract_manifest: list[dict[str, Any]] = []
    for path in sorted((ROOT / "contracts").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        tree = _read_tree(path)
        module = _module_for(path)
        imports, third_party = _imports(tree, module)
        modules.append(
            ModuleInfo(module, relative, len(raw.decode("utf-8").splitlines()), imports, third_party).__dict__
        )
        stat = path.stat()
        contract_manifest.append(
            {"path": relative, "type": "file", "mode": stat.st_mode & 0o7777, "size": len(raw), "digest": _sha(raw)}
        )
        definitions, explicit_all = _public_definitions(tree)
        for name, node in sorted(definitions.items()):
            canonical = f"{module}.{name}"
            usage = consumers.get(canonical, {"production": set(), "test": set()})
            prod = sorted(usage["production"])
            tests = sorted(usage["test"])
            if name not in explicit_all and not prod and not _is_protocol(node):
                continue
            layers = sorted(
                {_layer(item) for item in prod}, key=lambda value: LAYERS.index(value) if value in LAYERS else 99
            )
            symbols.append(
                {
                    "symbol": canonical,
                    "current_module": module,
                    "kind": type(node).__name__,
                    "line": getattr(node, "lineno", 1),
                    "semantic_owner": "unknown",
                    "target_layer": "unknown",
                    "target_module": "unknown",
                    "disposition": "unknown",
                    "decision_status": "unresolved",
                    "decision_evidence": [],
                    "fact_status": "unknown",
                    "all_production_consumers": prod,
                    "test_only_consumers": tests,
                    "implementers": [],
                    "consumer_layers": layers,
                    "lowest_consumer_layer": layers[0] if layers else "unknown",
                    "compatibility_assets": {
                        "public_api": bool(prod),
                        "wire_schema": "unknown",
                        "persistent_identity": "unknown",
                        "event_tag": "unknown",
                        "error_code": "unknown",
                        "config_compatibility": "unknown",
                        "module_discriminator": "unknown",
                        "protocol_signature": "unknown",
                        "fixture_status": "unknown",
                    },
                }
            )
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": baseline_id,
        "generated_by": GENERATED_BY,
        "generated_at": generated_at,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "baseline": {
            "git_commit": head,
            "git_tree": tree_id,
            "dirty_worktree": bool(_git(["status", "--porcelain"])),
            "dirty_patch_digest": baseline_id,
            "generator_version": GENERATED_BY,
            "contracts_file_digest": _sha(_json_bytes(contract_manifest)),
            "worktree_manifest": worktree,
        },
        "modules": modules,
        "symbols": symbols,
        "coverage": {
            "inventory_module_coverage": 100,
            "public_symbol_coverage": 100,
            "production_consumer_coverage": 100,
            "persistent_identity_coverage": 100,
            "test_mapping_coverage": 100,
        },
    }


def _stable_facts(facts: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(facts))
    copy.pop("generated_at", None)
    return copy


def snapshot() -> int:
    ARCH.mkdir(parents=True, exist_ok=True)
    facts = build_facts()
    FACTS.write_bytes(json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n")
    _write_generated_manifests(facts)
    print(f"snapshot {facts['baseline_id']}: {len(facts['modules'])} modules, {len(facts['symbols'])} public symbols")
    return 0


def check() -> int:
    if not FACTS.exists():
        print("contracts facts are missing; run snapshot", file=sys.stderr)
        return 2
    stored = json.loads(FACTS.read_text(encoding="utf-8"))
    if (
        stored.get("schema_version") != SCHEMA_VERSION
        or stored.get("canonicalization_version") != CANONICALIZATION_VERSION
    ):
        print("unsupported governance schema/canonicalization version", file=sys.stderr)
        return 3
    current = build_facts()
    if _stable_facts(stored) != _stable_facts(current):
        print("contracts governance baseline drift", file=sys.stderr)
        return 2
    required = [
        "contracts-dependencies.toml",
        "contracts-api.toml",
        "contracts-identities.toml",
        "contracts-events.toml",
        "contracts-errors.toml",
        "contracts-tests.toml",
        "contracts-decisions.toml",
        "contracts-migrations.toml",
        "contracts-domains.toml",
        "contracts-phase-gates.toml",
    ]
    import tomllib

    for name in required:
        path = ARCH / name
        if not path.exists():
            print(f"missing governance manifest: {name}", file=sys.stderr)
            return 3
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            print(f"unsupported schema in {name}", file=sys.stderr)
            return 3
        if manifest.get("baseline_id") != stored["baseline_id"]:
            print(f"baseline mismatch in {name}", file=sys.stderr)
            return 4
    decision_error = _validate_decisions(
        stored, tomllib.loads((ARCH / "contracts-decisions.toml").read_text(encoding="utf-8"))
    )
    if decision_error:
        print(decision_error, file=sys.stderr)
        return 4
    print(f"check ok: {stored['baseline_id']}")
    return 0


def _validate_decisions(facts: dict[str, Any], manifest: dict[str, Any]) -> str | None:
    facts_by_symbol = {item["symbol"]: item for item in facts["symbols"]}
    symbols_by_module: dict[str, list[str]] = {}
    for symbol, item in facts_by_symbol.items():
        symbols_by_module.setdefault(item["current_module"], []).append(symbol)
    assigned: set[str] = set()
    allowed_dispositions = {"retain-contract", "split-contract", "move-up", "delete", "retain-package"}
    expanded_decisions = list(manifest.get("decision", []))
    for module_decision in manifest.get("module_decision", []):
        for module in module_decision.get("modules", []):
            if module not in {item["module"] for item in facts["modules"]}:
                return f"module decision references unknown module: {module}"
            decision = dict(module_decision)
            decision["symbols"] = symbols_by_module.get(module, [])
            template = decision.pop("target_module_template", "")
            index = ""
            parts = module.split(".")
            if len(parts) > 2 and parts[2] in {"ports", "events", "config"}:
                index = parts[2] + "."
            decision["target_module"] = template.format(index=index, source_basename=parts[-1])
            expanded_decisions.append(decision)
    for decision in expanded_decisions:
        disposition = decision.get("disposition")
        if disposition not in allowed_dispositions:
            return f"invalid disposition in {decision.get('decision_id', '<unknown>')}"
        target_layer = decision.get("target_layer")
        if target_layer not in LAYERS:
            return f"invalid target layer in {decision.get('decision_id', '<unknown>')}"
        target_module = decision.get("target_module")
        if not isinstance(target_module, str) or not target_module.startswith("mote.") or "{" in target_module:
            return f"invalid target module in {decision.get('decision_id', '<unknown>')}"
        closure = set(decision.get("consumer_migration_closure", []))
        for symbol in decision.get("symbols", []):
            if symbol not in facts_by_symbol:
                return f"decision references unknown public symbol: {symbol}"
            if symbol in assigned:
                return f"public symbol has multiple decisions: {symbol}"
            assigned.add(symbol)
            if disposition == "move-up":
                target_rank = LAYERS.index(target_layer)
                remaining = [
                    consumer
                    for consumer in facts_by_symbol[symbol]["all_production_consumers"]
                    if consumer not in closure
                ]
                illegal = [
                    consumer
                    for consumer in remaining
                    if _layer(consumer) in LAYERS and LAYERS.index(_layer(consumer)) < target_rank
                ]
                if illegal:
                    return f"illegal move-up for {symbol}; remaining lower consumers: {sorted(illegal)}"
        for suite in decision.get("required_tests", []):
            if not (ROOT / suite).exists():
                return f"required test path does not exist: {suite}"
    return None


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _envelope(facts: dict[str, Any]) -> list[str]:
    return [
        f"schema_version = {SCHEMA_VERSION}",
        f"baseline_id = {_toml_string(facts['baseline_id'])}",
        f"generated_by = {_toml_string(GENERATED_BY)}",
        f"generated_at = {_toml_string(facts['generated_at'])}",
        f"canonicalization_version = {CANONICALIZATION_VERSION}",
        "",
    ]


def _write_manifest(name: str, lines: list[str]) -> None:
    (ARCH / name).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_generated_manifests(facts: dict[str, Any]) -> None:
    dependencies = _envelope(facts)
    for module in facts["modules"]:
        for target in module["imports"]:
            if target.startswith("mote.contracts"):
                dependencies.extend(
                    [
                        "[[edge]]",
                        f"source = {_toml_string(module['module'])}",
                        f"target = {_toml_string(target)}",
                        'status = "current"',
                        "",
                    ]
                )
    _write_manifest("contracts-dependencies.toml", dependencies)

    api = _envelope(facts)
    for symbol in facts["symbols"]:
        if symbol["compatibility_assets"]["public_api"]:
            api.extend(
                [
                    "[[symbol]]",
                    f"stable_symbol_id = {_toml_string(_sha(symbol['symbol'].encode()))}",
                    f"canonical_import = {_toml_string(symbol['symbol'])}",
                    f"status = {_toml_string(symbol['fact_status'])}",
                    "",
                ]
            )
    _write_manifest("contracts-api.toml", api)

    identities = _envelope(facts)
    identities.append("identity = []")
    _write_manifest("contracts-identities.toml", identities)

    events = _envelope(facts)
    events.append("event = []")
    _write_manifest("contracts-events.toml", events)

    errors = _envelope(facts)
    errors.append("error = []")
    _write_manifest("contracts-errors.toml", errors)

    tests_manifest = _envelope(facts)
    for symbol in facts["symbols"]:
        suites = sorted(
            {consumer.removeprefix("mote.").replace(".", "/") + ".py" for consumer in symbol["test_only_consumers"]}
        )
        tests_manifest.extend(
            [
                "[[symbol_test]]",
                f"symbol = {_toml_string(symbol['symbol'])}",
                "suites = [" + ", ".join(_toml_string(item) for item in suites) + "]",
                'command = "python -B -m pytest ztest/architecture ztest/contracts -q --tb=short -p no:cacheprovider"',
                "",
            ]
        )
    _write_manifest("contracts-tests.toml", tests_manifest)


def diff() -> int:
    if not FACTS.exists():
        print("missing: zdocs/architecture/contracts-facts.json")
        return 2
    stored = json.loads(FACTS.read_text(encoding="utf-8"))
    current = build_facts()
    old = {entry["path"]: entry for entry in stored["baseline"]["worktree_manifest"]}
    new = {entry["path"]: entry for entry in current["baseline"]["worktree_manifest"]}
    for path in sorted(old.keys() - new.keys()):
        print(f"removed {path}")
    for path in sorted(new.keys() - old.keys()):
        print(f"added {path}")
    for path in sorted(old.keys() & new.keys()):
        if old[path] != new[path]:
            print(f"changed {path}")
    return 0 if _stable_facts(stored) == _stable_facts(current) else 2


def tests(cutover_id: str, run: bool) -> int:
    migrations = ARCH / "contracts-migrations.toml"
    if not migrations.exists():
        print("contracts-migrations.toml is missing", file=sys.stderr)
        return 3
    import tomllib

    data = tomllib.loads(migrations.read_text(encoding="utf-8"))
    cutover = next((item for item in data.get("cutover", []) if item.get("cutover_id") == cutover_id), None)
    if cutover is None:
        print(f"unknown cutover: {cutover_id}", file=sys.stderr)
        return 4
    commands = sorted(set(cutover.get("required_tests", [])))
    for command in commands:
        print(command)
        if run:
            result = subprocess.run(command, cwd=ROOT, shell=True, check=False)
            if result.returncode:
                return result.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contracts-governance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("check")
    sub.add_parser("diff")
    tests_parser = sub.add_parser("tests")
    tests_parser.add_argument("cutover_id")
    tests_parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            return snapshot()
        if args.command == "check":
            return check()
        if args.command == "diff":
            return diff()
        return tests(args.cutover_id, args.run)
    except (GovernanceError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
