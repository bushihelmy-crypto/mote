"""Low-resource static architecture gates with no package import side effects."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")
APPROVED_FACT_ENCODERS = {
    "product/inference/daemon/operations_audit_codec.py",
    "runtime/session/codec.py",
}
GOVERNED_TYPED_PATHS = (
    "contracts/composition",
    "contracts/ports/events/telemetry.py",
    "contracts/ports/code_intelligence/lsp.py",
    "contracts/ports/task/operations.py",
    "product/composition",
    "product/lsp/factory.py",
    "runtime/events/telemetry.py",
)


def production_paths():
    for root in PRODUCTION_ROOTS:
        yield from (ROOT / root).rglob("*.py")


def qualified_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def check_local_imports() -> list[str]:
    violations: list[str] = []
    for path in production_paths():
        relative = path.relative_to(ROOT).as_posix()
        parents: dict[ast.AST, ast.AST] = {}
        tree = _tree(path)
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            parent = parents.get(node)
            while parent is not None and not isinstance(parent, ast.Module):
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    violations.append(f"{relative}:{node.lineno}: nested import")
                    break
                parent = parents.get(parent)
    return violations


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("mote", *parts))


def _resolve_from(source: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = source.split(".")[:-1]
    prefix = package[: len(package) - node.level + 1]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _product_import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for path in (ROOT / "product").rglob("*.py"):
        source = _module_name(path)
        graph[source]
        for node in ast.walk(_tree(path)):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_from(source, node)
                if target:
                    targets.append(target)
            graph[source].update(target for target in targets if target.startswith("mote.product"))
    return graph


def _unit(module: str, depth: int) -> str | None:
    parts = module.split(".")
    if parts[:2] != ["mote", "product"] or len(parts) < 3:
        return None
    product_parts = parts[2:]
    if depth == 1:
        return product_parts[0]
    if len(product_parts) >= 2 and (ROOT / "product" / product_parts[0] / product_parts[1]).is_dir():
        return ".".join(product_parts[:2])
    return product_parts[0]


def _components(graph: dict[str, set[str]]) -> set[tuple[str, ...]]:
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    stacked: set[str] = set()
    result: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        indexes[node] = lowlinks[node] = len(indexes)
        stack.append(node)
        stacked.add(node)
        for target in graph[node]:
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in stacked:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            stacked.remove(target)
            component.append(target)
            if target == node:
                break
        if len(component) > 1:
            result.add(tuple(sorted(component)))

    for node in tuple(graph):
        if node not in indexes:
            visit(node)
    return result


def check_product_scc() -> list[str]:
    imports = _product_import_graph()
    violations: list[str] = []
    for depth in (1, 2):
        graph: dict[str, set[str]] = defaultdict(set)
        for source, targets in imports.items():
            source_unit = _unit(source, depth)
            if source_unit is None or (depth == 2 and "." not in source_unit):
                continue
            graph[source_unit]
            for target in targets:
                target_unit = _unit(target, depth)
                if target_unit is None or target_unit == source_unit:
                    continue
                if depth == 1 or _unit(source, 1) == _unit(target, 1):
                    graph[source_unit].add(target_unit)
        violations.extend(f"depth {depth} SCC: {', '.join(component)}" for component in sorted(_components(graph)))
    return violations


def check_telemetry_erasure() -> list[str]:
    owner = ROOT / "runtime/events/telemetry.py"
    tree = _tree(owner)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    violations: list[str] = []
    if "_TypedTelemetryBinding" not in classes:
        violations.append("runtime/events/telemetry.py: private erased binding missing")
    runtime = classes.get("TelemetryRuntime")
    if runtime and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "subscribe_raw"
        for node in runtime.body
    ):
        violations.append("runtime/events/telemetry.py: subscribe_raw remains public")
    for path in production_paths():
        if path == owner:
            continue
        source = path.read_text(encoding="utf-8")
        if "_TypedTelemetryBinding" in source or "TypedTelemetryBinding" in source:
            violations.append(f"{path.relative_to(ROOT)}: telemetry erasure leaked")
    return violations


def check_fact_admission() -> list[str]:
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in production_paths()
        if any(
            isinstance(node, ast.Call) and qualified_name(node.func).endswith("UncommittedFact")
            for node in ast.walk(_tree(path))
        )
    }
    return [
        *(f"unapproved constructor: {path}" for path in sorted(actual - APPROVED_FACT_ENCODERS)),
        *(f"declared encoder missing constructor: {path}" for path in sorted(APPROVED_FACT_ENCODERS - actual)),
    ]


def check_dynamic_discovery() -> list[str]:
    violations: list[str] = []
    forbidden_calls = {"importlib.import_module", "pkgutil.walk_packages", "__import__"}
    approved_importers: set[str] = set()
    for path in production_paths():
        relative = path.relative_to(ROOT).as_posix()
        if relative in approved_importers:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
                violations.append(f"{relative}:{node.lineno}: dynamic __getattr__")
            if isinstance(node, ast.Call) and qualified_name(node.func) in forbidden_calls:
                violations.append(f"{relative}:{node.lineno}: {qualified_name(node.func)}")
    return violations


def check_governed_boundary() -> list[str]:
    violations: list[str] = []
    for relative in GOVERNED_TYPED_PATHS:
        path = ROOT / relative
        paths = path.rglob("*.py") if path.is_dir() else (path,)
        for candidate in paths:
            for node in ast.walk(_tree(candidate)):
                if isinstance(node, ast.Name) and node.id == "Any":
                    violations.append(f"{candidate.relative_to(ROOT)}:{node.lineno}: Any")
    for path in production_paths():
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and qualified_name(node.func) in {"cast", "typing.cast"}
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "Any"
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: cast(Any)")
    return violations


def check_stable_definition_identity() -> list[str]:
    """Semantic identities must not depend on Python location or object identity."""

    governed = {
        "orchestration/workflows/definition.py": {"_implementation_id"},
        "runtime/tools/definition_compiler.py": {
            "python_tool_source_identity",
            "compile_tool_source_identity",
            "_content_identity",
        },
    }
    forbidden_attributes = {"__module__", "__qualname__", "co_filename"}
    forbidden_calls = {"id", "inspect.getsource", "getattr"}
    violations: list[str] = []
    for relative, function_names in governed.items():
        for function in (
            node
            for node in ast.walk(_tree(ROOT / relative))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in function_names
        ):
            for node in ast.walk(function):
                if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
                    violations.append(f"{relative}:{node.lineno}: identity uses {node.attr}")
                if isinstance(node, ast.Call) and qualified_name(node.func) in forbidden_calls:
                    violations.append(f"{relative}:{node.lineno}: identity uses {qualified_name(node.func)}")
    return violations


def check_message_boundary_ownership() -> list[str]:
    tree = _tree(ROOT / "contracts/conversation/messages.py")
    message = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Message")
    forbidden = {"to_dict", "dump", "from_dict", "load"}
    return [
        f"contracts/conversation/messages.py:{node.lineno}: Message owns adapter method {node.name}"
        for node in message.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden
    ]


def check_session_decoder_strictness() -> list[str]:
    governed = (
        "runtime/session/events.py",
        "product/inference/backends/sqlite.py",
    )
    violations: list[str] = []
    for relative in governed:
        tree = _tree(ROOT / relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and qualified_name(node.func) in {"asdict", "vars"}:
                violations.append(f"{relative}:{node.lineno}: automatic durable payload encoder")
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or "from_" not in node.name:
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and qualified_name(child.func) in {"str", "int", "bool"}:
                    violations.append(f"{relative}:{child.lineno}: durable decoder primitive coercion")
    return violations


def check_explicit_durable_event_codecs() -> list[str]:
    violations: list[str] = []
    base_path = ROOT / "contracts/events/_base.py"
    for node in ast.walk(_tree(base_path)):
        if isinstance(node, ast.Call) and qualified_name(node.func) in {"vars", "asdict"}:
            violations.append(f"contracts/events/_base.py:{node.lineno}: automatic event payload encoder")
    for relative in ("contracts/events/output.py", "contracts/events/model.py", "contracts/events/conversation.py"):
        for node in _tree(ROOT / relative).body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(qualified_name(base).endswith("DurableFact") for base in node.bases):
                continue
            methods = {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for required in ("payload", "from_payload"):
                if required not in methods:
                    violations.append(f"{relative}:{node.lineno}: {node.name} lacks explicit {required}")
    return violations


def check_kernel_canonical_inputs() -> list[str]:
    violations: list[str] = []
    tokenization = _tree(ROOT / "kernel/inference/tokenization.py")
    if any(isinstance(node, ast.Name) and node.id == "Any" for node in ast.walk(tokenization)):
        violations.append("kernel/inference/tokenization.py: tokenization accepts Any")
    for relative in ("kernel/commands/channel.py", "kernel/commands/native.py", "kernel/commands/xml/channel.py"):
        for node in ast.walk(_tree(ROOT / relative)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "iter_commands":
                violations.append(f"{relative}:{node.lineno}: legacy dynamic command mapping entrypoint")
    recovery_source = (ROOT / "kernel/commands/xml/recovery.py").read_text(encoding="utf-8")
    if "list[dict]" in recovery_source:
        violations.append("kernel/commands/xml/recovery.py: XML decoder returns dynamic mappings")
    return violations


def _defined_symbols(tree: ast.Module) -> set[str]:
    symbols: set[str] = set()

    def visit(body: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = (*prefix, node.name)
                symbols.add(".".join(qualified))
                visit(node.body, qualified)

    visit(tree.body)
    return symbols


def _awaitable_probe_symbols(path: Path) -> set[str]:
    found: set[str] = set()

    def visit(body: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualified = (*prefix, node.name)
                visit(node.body, qualified)
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = (*prefix, node.name)
                if any(
                    isinstance(child, ast.Call) and qualified_name(child.func) in {"inspect.isawaitable", "isawaitable"}
                    for child in ast.walk(node)
                ):
                    found.add(".".join(qualified))
                visit(node.body, qualified)

    visit(_tree(path).body)
    return found


def check_dynamic_boundary_registry() -> list[str]:
    """Validate exact long-lived dynamic boundaries and forbid unregistered probing."""

    registry_path = ROOT / "typecheck/dynamic-boundaries.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"typecheck/dynamic-boundaries.json: invalid registry: {error}"]
    if type(payload) is not dict or set(payload) != {"schema_version", "boundaries"}:
        return ["typecheck/dynamic-boundaries.json: invalid envelope"]
    if payload["schema_version"] != 2 or type(payload["boundaries"]) is not list:
        return ["typecheck/dynamic-boundaries.json: unsupported schema"]

    fields = {
        "name",
        "file",
        "qualified_symbol",
        "category",
        "source",
        "validation",
        "validation_test",
        "owner",
        "review_after",
    }
    violations: list[str] = []
    identities: set[tuple[str, str, str]] = set()
    registered_probes: set[tuple[str, str]] = set()
    for index, record in enumerate(payload["boundaries"]):
        label = f"typecheck/dynamic-boundaries.json:boundaries[{index}]"
        if type(record) is not dict or set(record) != fields:
            violations.append(f"{label}: fields are not exact")
            continue
        if any(type(record[field]) is not str or not record[field].strip() for field in fields):
            violations.append(f"{label}: every field must be a non-empty string")
            continue
        relative = record["file"]
        symbol = record["qualified_symbol"]
        identity = (relative, symbol, record["category"])
        if identity in identities:
            violations.append(f"{label}: duplicate boundary identity {identity}")
        identities.add(identity)
        source_path = ROOT / relative
        test_path = ROOT / record["validation_test"]
        if not source_path.is_file() or relative.split("/", 1)[0] not in PRODUCTION_ROOTS:
            violations.append(f"{label}: production file is missing or out of scope")
            continue
        if not test_path.is_file() or not record["validation_test"].startswith("ztest/"):
            violations.append(f"{label}: validation test is missing or out of scope")
        if symbol not in _defined_symbols(_tree(source_path)):
            violations.append(f"{label}: qualified symbol {symbol!r} is stale")
        try:
            review_after = date.fromisoformat(record["review_after"])
        except ValueError:
            violations.append(f"{label}: review_after is not an ISO date")
        else:
            if review_after <= date.today():
                violations.append(f"{label}: boundary review has expired")
        if record["category"] == "external-sdk-lifecycle":
            registered_probes.add((relative, symbol))

    actual_probes: set[tuple[str, str]] = set()
    for path in production_paths():
        relative = path.relative_to(ROOT).as_posix()
        actual_probes.update((relative, symbol) for symbol in _awaitable_probe_symbols(path))
    violations.extend(
        f"{relative}:{symbol}: unregistered runtime awaitable probing"
        for relative, symbol in sorted(actual_probes - registered_probes)
    )
    violations.extend(
        f"{relative}:{symbol}: registered awaitable probe is stale"
        for relative, symbol in sorted(registered_probes - actual_probes)
    )
    return violations


def _references_evidence(source: str, evidence_tokens: set[str]) -> bool:
    """Return whether source names at least one registered evidence identity."""

    return any(token in source for token in evidence_tokens)


def check_confirmed_dynamic_debt_symbols() -> list[str]:
    """Validate the exact closure-evidence manifest for confirmed D0-D38 debt."""

    baseline_path = ROOT / "typecheck/dynamic-type-debt.json"
    expected_ids = tuple(f"D{index}" for index in range(39) if index not in {24, 30, 33})
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"typecheck/dynamic-type-debt.json: invalid baseline: {error}"]
    if type(baseline) is not dict or set(baseline) != {
        "schema_version",
        "ruleset_version",
        "source_roots",
        "governed_ids",
        "records",
    }:
        return ["typecheck/dynamic-type-debt.json: invalid envelope"]
    violations: list[str] = []
    if baseline["schema_version"] != 2:
        violations.append("typecheck/dynamic-type-debt.json: unsupported schema")
    if baseline["ruleset_version"] != "mote.generic-dynamic-type-debt/v2":
        violations.append("typecheck/dynamic-type-debt.json: wrong ruleset identity")
    if baseline["source_roots"] != list(PRODUCTION_ROOTS):
        violations.append("typecheck/dynamic-type-debt.json: source roots drifted")
    if baseline["governed_ids"] != list(expected_ids):
        violations.append("typecheck/dynamic-type-debt.json: governed ID set drifted")
    records = baseline["records"]
    if type(records) is not list:
        violations.append("typecheck/dynamic-type-debt.json: records must be a list")
        records = []
    record_ids: list[str] = []
    required_record_fields = {"id", "owner", "symbols", "consumers", "validation_tests"}
    for index, record in enumerate(records):
        label = f"typecheck/dynamic-type-debt.json:records[{index}]"
        if type(record) is not dict or set(record) != required_record_fields:
            violations.append(f"{label}: invalid closure evidence shape")
            continue
        debt_id = record["id"]
        if type(debt_id) is not str:
            violations.append(f"{label}: id must be a string")
            continue
        record_ids.append(debt_id)
        for field_name in ("owner", "symbols", "consumers", "validation_tests"):
            values = record[field_name]
            if type(values) is not list or not values or any(type(value) is not str or not value for value in values):
                violations.append(f"{label}: {field_name} must contain non-empty strings")
                continue
            for value in values:
                path_text = value.split("::", 1)[0]
                path = ROOT / path_text
                if not path.is_file():
                    violations.append(f"{label}: stale {field_name} path: {path_text}")
                    continue
                if field_name in {"owner", "symbols"} and "::" in value:
                    symbol = value.split("::", 1)[1]
                    names = _defined_symbols(_tree(path))
                    if symbol not in names:
                        violations.append(f"{label}: stale symbol identity: {value}")
        owner_and_symbols = record.get("owner", []) + record.get("symbols", [])
        evidence_tokens = {
            part
            for value in owner_and_symbols
            if type(value) is str and "::" in value
            for part in (
                value.split("::", 1)[1].split(".", 1)[0],
                value.rsplit(".", 1)[-1],
            )
            if len(part) >= 4
        }
        owner_module_tokens = {Path(value.split("::", 1)[0]).stem for value in owner_and_symbols if type(value) is str}
        consumer_tokens = {
            Path(value.split("::", 1)[0]).stem for value in record.get("consumers", []) if type(value) is str
        }
        consumer_sources = [
            (value, (ROOT / value.split("::", 1)[0]).read_text(encoding="utf-8"))
            for value in record.get("consumers", [])
            if type(value) is str and (ROOT / value.split("::", 1)[0]).is_file()
        ]
        validation_sources = [
            (value, (ROOT / value.split("::", 1)[0]).read_text(encoding="utf-8"))
            for value in record.get("validation_tests", [])
            if type(value) is str and (ROOT / value.split("::", 1)[0]).is_file()
        ]
        if not consumer_sources:
            violations.append(f"{label}: consumer evidence is empty")
        if not validation_sources:
            violations.append(f"{label}: validation evidence is empty")
        dependency_tokens = evidence_tokens | owner_module_tokens
        for consumer, source in consumer_sources:
            if debt_id == "D21":
                config = baseline_path.parent.parent / "pyrightconfig.json"
                config_data = json.loads(config.read_text(encoding="utf-8"))
                excluded = tuple(config_data.get("exclude", ()))
                consumer_path = consumer.split("::", 1)[0]
                if any(consumer_path == item or consumer_path.startswith(f"{item.rstrip('/')}/") for item in excluded):
                    violations.append(f"{label}: Pyright consumer is excluded: {consumer}")
                continue
            if dependency_tokens and not _references_evidence(source, dependency_tokens):
                violations.append(f"{label}: consumer does not reference registered owner/symbol: {consumer}")
        validation_tokens = dependency_tokens | consumer_tokens
        for validation_test, source in validation_sources:
            if debt_id == "D21":
                if '"pyright"' not in source:
                    violations.append(f"{label}: D21 validation does not execute Pyright: {validation_test}")
                continue
            if validation_tokens and not _references_evidence(source, validation_tokens):
                violations.append(f"{label}: validation test is unrelated to registered evidence: {validation_test}")
    if record_ids != list(expected_ids):
        violations.append("typecheck/dynamic-type-debt.json: closure evidence ID order/set drifted")

    forbidden: dict[str, tuple[str, ...]] = {
        "kernel/commands/channel.py": (
            "history_projection(messages: list[Any])",
            "hasattr(message,",
            "default=str",
        ),
        "runtime/models/failover/orchestrator.py": ("Any", "dict[str, object]"),
        "contracts/hook/invocation.py": ("dict[str, Any]", "Mapping[str, Any]"),
        "contracts/model/profile.py": ("json_schema_transformer",),
        "orchestration/background_tasks/decorators.py": (
            "_background_task",
            "setattr(",
            "getattr(",
        ),
        "runtime/tools/tool_pipeline.py": ("tool: Any", "Callable[...,", "inspect.signature"),
        "runtime/fileops/mutation/artifact_roots.py": ("callable(getattr",),
        "runtime/tools/capability_types.py": (),
        "orchestration/workflows/graph.py": ("Callable[..., Any]", "dict[str, dict]", "Reducer[Any]"),
        "orchestration/workflows/types.py": (
            "Coroutine",
            "poll: Optional[Callable[[Any]",
            "submit: Coroutine",
        ),
        "orchestration/workflows/control.py": ("Any", "completed: set", "run_state: object"),
        "orchestration/workflows/deferred.py": ("Any", "Coroutine", "graph_ref: object", "state: object"),
        "orchestration/workflows/base_node.py": ("dict[str, dict]", "-> type | None"),
        "runtime/tools/provider.py": ("AnyToolset", "ToolDefinition[Any]"),
        "runtime/tools/provider_definitions.py": ("ToolDefinition[Any]", "Generic[CapabilityT]"),
        "contracts/tool/actions.py": ("dict[str, Any]", "Mapping[str, Any]"),
        "contracts/tool/policy.py": ("dict[str, Any]", "Mapping[str, Any]"),
        "runtime/tools/tool_executor.py": ("arguments: dict[str, Any]", "arguments: Mapping[str, Any]"),
        "contracts/foundation/errors/base.py": ("pickle", "__dict__.update"),
        "contracts/conversation/messages.py": ("SerializeAsAny",),
        "orchestration/background_tasks/model.py": ("task_id: str",),
        "contracts/ports/task/operations.py": ("task_id: str",),
        "orchestration/background_tasks/pool.py": ("task_id: str",),
        "product/agents/background_tasks.py": ("task_id: str",),
        "product/workflows/run_graph/compiler.py": ('extra="allow"', "extra='allow'"),
        "runtime/agent/components/context.py": ("lambda: role", "lambda role=role"),
        "product/interfaces/inference_api/application.py": ("AppKey[object]",),
        "product/interfaces/inference_webhook_api/application.py": ("AppKey[object]",),
    }
    for relative, needles in forbidden.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        for needle in needles:
            if needle in source:
                violations.append(f"{relative}: confirmed debt pattern remains: {needle}")

    for path in (ROOT / "contracts/events").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for needle in ("dict[str, Any]", "Dict[str, Any]", "Mapping[str, Any]"):
            if needle in source:
                violations.append(f"{path.relative_to(ROOT).as_posix()}: D3 public event boundary retains {needle}")

    for relative in (
        "orchestration/workflows/definition.py",
        "orchestration/workflows/engine.py",
        "orchestration/workflows/notify.py",
    ):
        tree = _tree(ROOT / relative)
        for node in ast.walk(tree):
            annotation = None
            if isinstance(node, ast.arg):
                annotation = node.annotation
            elif isinstance(node, ast.AnnAssign):
                annotation = node.annotation
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotation = node.returns
            if annotation is not None and "Any" in ast.unparse(annotation):
                violations.append(f"{relative}:{node.lineno}: D7 Workflow main chain retains Any")

    workflow_types_source = (ROOT / "orchestration/workflows/types.py").read_text(encoding="utf-8")
    if "async def execute(self) -> object" in workflow_types_source:
        violations.append("orchestration/workflows/types.py: D15 Stage result is erased to object")
    deferred_source = (ROOT / "orchestration/workflows/deferred.py").read_text(encoding="utf-8")
    for needle in ("graph_ref:", 'state: "GraphState', "WorkflowPollFactory = Callable[[], Awaitable[object]]"):
        if needle in deferred_source:
            violations.append(f"orchestration/workflows/deferred.py: D35/D15 retired boundary remains: {needle}")
    inspection_source = (ROOT / "product/workflows/inspection.py").read_text(encoding="utf-8")
    for needle in ("run: WorkflowRun", "graph_meta:", "executable._graph"):
        if needle in inspection_source:
            violations.append(
                f"product/workflows/inspection.py: D35 live execution object leaks to presentation: {needle}"
            )

    workflow_types = _tree(ROOT / "orchestration/workflows/types.py")
    stage = next(
        (node for node in workflow_types.body if isinstance(node, ast.ClassDef) and node.name == "Stage"),
        None,
    )
    if stage is None:
        violations.append("orchestration/workflows/types.py: canonical Stage is missing")
    else:
        retired_stage_fields = {"poll", "name", "timeout"}
        for node in ast.walk(stage):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in retired_stage_fields:
                    violations.append(
                        f"orchestration/workflows/types.py:{node.lineno}: "
                        f"Stage retains retired public field {node.target.id}"
                    )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    if argument.arg in retired_stage_fields:
                        violations.append(
                            f"orchestration/workflows/types.py:{argument.lineno}: "
                            f"Stage retains retired public parameter {argument.arg}"
                        )

    capability_tree = _tree(ROOT / "runtime/tools/capability_types.py")
    for node in ast.walk(capability_tree):
        if not isinstance(node, ast.Subscript) or qualified_name(node.value).split(".")[-1] != "Callable":
            continue
        if ast.unparse(node).startswith("Callable[..., ") or ast.unparse(node).startswith("Callable[...,"):
            violations.append(
                f"runtime/tools/capability_types.py:{node.lineno}: formal capability retains Callable ellipsis"
            )

    for relative in ("contracts/events/model.py", "contracts/events/session.py", "contracts/events/telemetry.py"):
        tree = _tree(ROOT / relative)
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for node in class_node.body:
                if not isinstance(node, ast.AnnAssign):
                    continue
                annotation = ast.unparse(node.annotation)
                if annotation in {"Any", "dict", "Mapping"} or "[str, Any]" in annotation:
                    violations.append(
                        f"{relative}:{node.lineno}: public event field has dynamic annotation {annotation}"
                    )

    gateway_classes = {
        "GenerationBoundRuntimeModelGateway",
        "ExactCachedModelGateway",
        "CurrentRuntimeModelGateway",
    }
    for relative in (
        "runtime/models/model_gateway.py",
        "runtime/models/cached_gateway.py",
        "runtime/models/composition_context.py",
    ):
        for node in _tree(ROOT / relative).body:
            if not isinstance(node, ast.ClassDef) or node.name not in gateway_classes:
                continue
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.args.kwarg is not None:
                    violations.append(f"{relative}:{method.lineno}: {node.name}.{method.name} retains **kwargs")
    return violations


def check_derived_artifact() -> list[str]:
    artifacts = (
        ("ztest/architecture/governance_artifact.py", "zdocs/architecture/dynamic-boundary-governance-v1.json"),
        (
            "ztest/architecture/requirement_evidence.py",
            "zdocs/architecture/dynamic-boundary-requirement-evidence-v1.json",
        ),
    )
    violations: list[str] = []
    for generator, target_name in artifacts:
        completed = subprocess.run(
            [sys.executable, "-B", generator],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            violations.append(f"{generator}: generator exited with status {completed.returncode}")
            continue
        target = ROOT / target_name
        try:
            generated = json.loads(completed.stdout)
            committed = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            violations.append(f"{target_name}: artifact could not be compared: {error}")
            continue
        if generated != committed:
            violations.append(f"stale artifact: {target_name}")
    return violations


CHECKS = {
    "local-imports": check_local_imports,
    "product-scc": check_product_scc,
    "telemetry-erasure": check_telemetry_erasure,
    "fact-admission": check_fact_admission,
    "dynamic-discovery": check_dynamic_discovery,
    "derived-artifact": check_derived_artifact,
    "governed-boundary": check_governed_boundary,
    "stable-definition-identity": check_stable_definition_identity,
    "message-boundary-ownership": check_message_boundary_ownership,
    "session-decoder-strictness": check_session_decoder_strictness,
    "explicit-durable-event-codecs": check_explicit_durable_event_codecs,
    "kernel-canonical-inputs": check_kernel_canonical_inputs,
    "dynamic-boundary-registry": check_dynamic_boundary_registry,
    "confirmed-dynamic-debt-symbols": check_confirmed_dynamic_debt_symbols,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=tuple(CHECKS))
    arguments = parser.parse_args()
    violations = CHECKS[arguments.check]()
    if violations:
        print("\n".join(sorted(violations)))
        return 1
    print(f"{arguments.check} architecture invariant is closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
