"""Product package ownership and dependency-direction guardrails."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROOT = PACKAGE_ROOT / "product"

# These are migration facts, not an extensible allowlist. Delete each exact
# component as soon as its owning phase removes the cycle.
EXPECTED_BOUNDED_CONTEXT_CYCLES: set[tuple[str, ...]] = set()
EXPECTED_PACKAGE_CYCLES: set[tuple[str, ...]] = set()

MIGRATION_FORBIDDEN_EDGES: set[tuple[str, str]] = set()

FORBIDDEN_TARGETS = {
    "config": {
        "agents",
        "toolsets",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "models": {
        "toolsets",
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "media_generation": {
        "toolsets",
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "web_search": {
        "toolsets",
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "lsp": {
        "toolsets",
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "routing": {
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "skills": {
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "code_map": {
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "toolsets": {
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "agents": {
        "config",
        "composition",
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "composition": {
        "interaction",
        "session_hosting",
        "presentation",
        "interfaces",
        "entrypoints",
    },
    "presentation": {
        "agents",
        "composition",
        "interaction",
        "session_hosting",
        "interfaces",
        "entrypoints",
    },
    "interaction": {"session_hosting", "interfaces", "entrypoints"},
    "session_hosting": {"interfaces", "entrypoints"},
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("mote", *parts))


def _resolve_from(source: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = source.split(".")[:-1]
    keep = len(package) - node.level + 1
    prefix = package[:keep]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


class _ProductImports(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.imports: list[tuple[int, str]] = []
        self.dynamic_violations: list[int] = []

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend((node.lineno, alias.name) for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        target = _resolve_from(self.source, node)
        if target:
            self.imports.append((node.lineno, target))

    def visit_Call(self, node: ast.Call) -> None:
        is_import_module = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
        )
        is_dunder_import = isinstance(node.func, ast.Name) and node.func.id == "__import__"
        if is_import_module or is_dunder_import:
            argument = node.args[0] if node.args else None
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                self.imports.append((node.lineno, argument.value))
            else:
                self.dynamic_violations.append(node.lineno)
        self.generic_visit(node)


def _imports() -> tuple[dict[str, set[str]], list[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    dynamic_violations: list[str] = []
    for path in PRODUCT_ROOT.rglob("*.py"):
        source = _module_name(path)
        visitor = _ProductImports(source)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for _, target in visitor.imports:
            if target.startswith("mote.product"):
                graph[source].add(target)
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        dynamic_violations.extend(f"{relative}:{line}" for line in visitor.dynamic_violations)
    return graph, dynamic_violations


def _unit(module: str, depth: int) -> str | None:
    parts = module.split(".")
    if parts[:2] != ["mote", "product"] or len(parts) < 3:
        return None
    product_parts = parts[2:]
    if depth == 1:
        return product_parts[0]
    if product_parts[0] in {"application", "container"}:
        return product_parts[0]
    if len(product_parts) >= 2 and (PRODUCT_ROOT / product_parts[0] / product_parts[1]).is_dir():
        return ".".join(product_parts[:2])
    return product_parts[0]


def _unit_graph(depth: int) -> dict[str, set[str]]:
    imports, _ = _imports()
    graph: dict[str, set[str]] = defaultdict(set)
    for source, targets in imports.items():
        source_unit = _unit(source, depth)
        if source_unit is None or (depth == 2 and "." not in source_unit):
            continue
        graph[source_unit]
        for target in targets:
            target_unit = _unit(target, depth)
            if depth == 2 and target_unit is not None and "." not in target_unit:
                continue
            same_context = _unit(source, 1) == _unit(target, 1)
            if target_unit is not None and target_unit != source_unit and (depth == 1 or same_context):
                graph[source_unit].add(target_unit)
    return graph


def _strong_components(graph: dict[str, set[str]]) -> set[tuple[str, ...]]:
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    stacked: set[str] = set()
    components: set[tuple[str, ...]] = set()

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
            components.add(tuple(sorted(component)))

    for node in tuple(graph):
        if node not in indexes:
            visit(node)
    return components


@pytest.mark.parametrize(
    ("depth", "expected"),
    ((1, EXPECTED_BOUNDED_CONTEXT_CYCLES), (2, EXPECTED_PACKAGE_CYCLES)),
)
def test_product_cycles_match_exact_migration_facts(depth: int, expected: set[tuple[str, ...]]) -> None:
    current = _strong_components(_unit_graph(depth))
    assert current == expected, (
        f"Product dependency cycles changed at depth {depth}. "
        "Delete resolved migration facts; never expand them.\n"
        f"expected={sorted(expected)}\nactual={sorted(current)}"
    )


def test_target_product_packages_follow_dependency_direction() -> None:
    violations: list[str] = []
    seen_migration_edges: set[tuple[str, str]] = set()
    imports, _ = _imports()
    for source, targets in imports.items():
        source_unit = _unit(source, 1)
        if source_unit is None:
            continue
        for target in targets:
            target_unit = _unit(target, 1)
            if target_unit in FORBIDDEN_TARGETS.get(source_unit, set()):
                edge = (source, target)
                if edge in MIGRATION_FORBIDDEN_EDGES:
                    seen_migration_edges.add(edge)
                else:
                    violations.append(f"{source} -> {target}")

            if source_unit == "interfaces" and target_unit == "interfaces":
                source_interface = _unit(source, 2)
                target_interface = _unit(target, 2)
                if source_interface != target_interface:
                    violations.append(f"{source} -> {target}")
    stale = MIGRATION_FORBIDDEN_EDGES - seen_migration_edges
    assert not stale, "Delete resolved Product migration edges:\n" + "\n".join(map(str, sorted(stale)))
    assert not violations, "Forbidden Product dependency edges:\n" + "\n".join(sorted(violations))


def test_product_dynamic_imports_are_statically_auditable() -> None:
    _, violations = _imports()
    assert not violations, "Product dynamic imports must be statically auditable:\n" + "\n".join(sorted(violations))


def test_presentation_internal_dependencies_stay_narrow() -> None:
    imports, _ = _imports()
    violations: list[str] = []
    for source, targets in imports.items():
        if source.startswith("mote.product.presentation.state"):
            forbidden = "mote.product.presentation.rich_rendering"
        elif source.startswith("mote.product.presentation.rich_rendering"):
            forbidden = "mote.product.presentation.state"
        else:
            continue
        violations.extend(f"{source} -> {target}" for target in targets if target.startswith(forbidden))
    assert not violations, "Presentation state/rendering coupling:\n" + "\n".join(sorted(violations))


def test_structured_interfaces_do_not_import_display_technology() -> None:
    imports, _ = _imports()
    violations = [
        f"{source} -> {target}"
        for source, targets in imports.items()
        if source.startswith(("mote.product.interfaces.acp", "mote.product.interfaces.agui"))
        for target in targets
        if target.startswith(
            (
                "mote.product.presentation.state",
                "mote.product.presentation.rich_rendering",
            )
        )
    ]
    assert not violations, "Structured interfaces import UI technology:\n" + "\n".join(sorted(violations))


def test_application_use_cases_do_not_depend_on_legacy_cli() -> None:
    imports, _ = _imports()
    violations = [
        f"{source} -> {target}"
        for source, targets in imports.items()
        if source.startswith(("mote.product.interaction", "mote.product.session_hosting"))
        for target in targets
        if target.startswith("mote.product.cli")
    ]
    assert not violations, "Application use cases import legacy CLI:\n" + "\n".join(sorted(violations))


def test_turn_execution_has_one_owner() -> None:
    owners: set[str] = set()
    for path in PRODUCT_ROOT.rglob("*.py"):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "send_input",
                "quiescent",
            }:
                owners.add(module)
            if isinstance(node.func, ast.Name) and node.func.id == "ErrorRaised":
                owners.add(module)
    assert owners == {"mote.product.interaction.turn"}


def test_legacy_cli_package_is_deleted() -> None:
    assert not (PRODUCT_ROOT / "cli").exists()
    imports, _ = _imports()
    violations = [
        f"{source} -> {target}"
        for source, targets in imports.items()
        for target in targets
        if target.startswith("mote.product.cli")
    ]
    assert not violations, "Legacy CLI imports remain:\n" + "\n".join(sorted(violations))


def test_external_capabilities_do_not_depend_on_model_facing_product() -> None:
    imports, _ = _imports()
    capability_prefixes = (
        "mote.product.models",
        "mote.product.media_generation",
        "mote.product.web_search",
        "mote.product.lsp",
    )
    violations = [
        f"{source} -> {target}"
        for source, targets in imports.items()
        if source.startswith(capability_prefixes)
        for target in targets
        if target.startswith("mote.product.toolsets")
    ]
    assert not violations, "Capability imports Toolsets:\n" + "\n".join(sorted(violations))


def test_generic_integration_and_error_packages_are_deleted() -> None:
    assert not (PRODUCT_ROOT / "integrations").exists()
    assert not (PRODUCT_ROOT / "errors").exists()


def test_paths_has_one_stable_facade_and_no_config_or_session_ownership() -> None:
    imports, _ = _imports()
    internal_prefixes = (
        "mote.product.paths.defaults",
        "mote.product.paths.discovery",
        "mote.product.paths.model",
    )
    violations = [
        f"{source} -> {target}"
        for source, targets in imports.items()
        if not source.startswith("mote.product.paths")
        for target in targets
        if target.startswith(internal_prefixes)
    ]
    assert not violations, "Callers bypass Product Paths facade:\n" + "\n".join(sorted(violations))

    path_sources = "\n".join(path.read_text(encoding="utf-8") for path in (PRODUCT_ROOT / "paths").glob("*.py"))
    forbidden_names = {
        "load_json_section",
        "load_mote_json_section",
        "SESSIONS_SUBDIR",
        "ROLLOUT_FILENAME",
        "DEFAULT_SESSION_BUCKET",
    }
    present = sorted(name for name in forbidden_names if name in path_sources)
    assert not present, f"Product Paths owns config/session semantics: {present}"


def test_config_sources_is_deleted_and_config_owns_adapters() -> None:
    assert not list((PRODUCT_ROOT / "config_sources").glob("*.py"))
    imports, _ = _imports()
    violations = [
        f"{source} -> {target}"
        for source, targets in imports.items()
        for target in targets
        if target.startswith("mote.product.config_sources")
    ]
    assert not violations, "Legacy config_sources imports remain:\n" + "\n".join(sorted(violations))
    assert {path.name for path in (PRODUCT_ROOT / "config" / "adapters").glob("*.py")} == {
        "__init__.py",
        "hooks.py",
        "mcp.py",
        "permissions.py",
    }


def test_runtime_does_not_import_product_paths() -> None:
    violations: list[str] = []
    runtime_root = PACKAGE_ROOT / "runtime"
    for path in runtime_root.rglob("*.py"):
        source = _module_name(path)
        visitor = _ProductImports(source)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        violations.extend(
            f"{path.relative_to(PACKAGE_ROOT)}:{line} -> {target}"
            for line, target in visitor.imports
            if target.startswith("mote.product.paths")
        )
    assert not violations, "Runtime imports Product Paths:\n" + "\n".join(sorted(violations))


def test_render_builder_domain_owners_stay_separate() -> None:
    builders = PRODUCT_ROOT / "presentation" / "rich_rendering" / "builders"
    assert not (builders / "core.py").exists()
    expected_owners = {
        "activity_header": "activity.py",
        "activity_outcome": "activity.py",
        "activity_topology": "activity.py",
        "compaction_summary_text": "message.py",
        "conversation_compacted_text": "message.py",
        "linkify": "message.py",
        "user_message_row": "message.py",
        "tool_started_text": "tool.py",
        "tool_completed_text": "tool.py",
        "tool_group_summary_text": "tool.py",
        "session_table": "session.py",
        "USAGE_SEP": "usage.py",
        "format_usage_line": "usage.py",
    }
    owners: dict[str, list[str]] = defaultdict(list)
    for path in builders.glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                owners[node.name].append(path.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                owners.update(
                    {
                        target.id: [*owners[target.id], path.name]
                        for target in targets
                        if isinstance(target, ast.Name) and target.id in expected_owners
                    }
                )
    assert {symbol: owner_files for symbol, owner_files in owners.items() if symbol in expected_owners} == {
        symbol: [filename] for symbol, filename in expected_owners.items()
    }


def test_read_notebook_adapter_has_one_owner_and_read_stays_the_tool_entry() -> None:
    builtin = PRODUCT_ROOT / "toolsets" / "builtin"
    read_module = builtin / "read.py"
    read_tree = ast.parse(read_module.read_text(encoding="utf-8"), filename=str(read_module))
    read_classes = [node.name for node in read_tree.body if isinstance(node, ast.ClassDef)]
    assert read_classes == ["Read"]
    read_imports = _ProductImports("mote.product.toolsets.builtin.read")
    read_imports.visit(read_tree)
    assert all(target != "json" for _, target in read_imports.imports)
    assert not {
        "_cell_source",
        "_render_outputs",
        "_render_notebook",
        "render_notebook",
    }.intersection(node.name for node in read_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))

    adapter = builtin / "read_adapters" / "notebook.py"
    adapter_tree = ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    assert {
        node.name
        for node in adapter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {
        "NotebookDecodeError",
        "NotebookJsonError",
        "_cell_source",
        "_render_outputs",
        "render_notebook",
        "parse_notebook",
    }


def test_read_image_processing_is_owned_by_media_adapter() -> None:
    builtin = PRODUCT_ROOT / "toolsets" / "builtin"
    read_module = builtin / "read.py"
    read_tree = ast.parse(read_module.read_text(encoding="utf-8"), filename=str(read_module))
    read_methods = {
        child.name
        for node in read_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Read"
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_prepare_image_bytes" not in read_methods
    read_imports = _ProductImports("mote.product.toolsets.builtin.read")
    read_imports.visit(read_tree)
    assert all(target != "PIL" for _, target in read_imports.imports)

    adapter = builtin / "read_adapters" / "image.py"
    adapter_tree = ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    assert {
        node.name
        for node in adapter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {"ImageProcessingError", "prepare_image"}


def test_read_text_formatting_is_owned_by_text_adapter() -> None:
    builtin = PRODUCT_ROOT / "toolsets" / "builtin"
    read_module = builtin / "read.py"
    read_tree = ast.parse(read_module.read_text(encoding="utf-8"), filename=str(read_module))
    read_functions = {node.name for node in read_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not {"_add_line_numbers", "add_line_numbers", "format_text_view"} & read_functions

    adapter = builtin / "read_adapters" / "text.py"
    adapter_tree = ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    assert {node.name for node in adapter_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))} == {
        "add_line_numbers",
        "format_text_view",
    }


def test_read_byte_formatting_is_owned_by_byte_adapter() -> None:
    builtin = PRODUCT_ROOT / "toolsets" / "builtin"
    read_module = builtin / "read.py"
    read_tree = ast.parse(read_module.read_text(encoding="utf-8"), filename=str(read_module))
    read_imports = _ProductImports("mote.product.toolsets.builtin.read")
    read_imports.visit(read_tree)
    assert all(target != "base64" for _, target in read_imports.imports)

    adapter = builtin / "read_adapters" / "byte.py"
    adapter_tree = ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    assert {node.name for node in adapter_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))} == {
        "format_byte_view"
    }


def test_read_pdf_view_formatting_is_owned_by_pdf_adapter() -> None:
    builtin = PRODUCT_ROOT / "toolsets" / "builtin"
    adapter = builtin / "read_adapters" / "pdf.py"
    adapter_tree = ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    assert {
        node.name
        for node in adapter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {
        "OversizedPdfPage",
        "pdf_view_data",
        "format_pdf_text",
        "prepare_pdf_render",
    }

    read_source = (builtin / "read.py").read_text(encoding="utf-8")
    assert "_MSG_DOCUMENT_EMPTY" not in read_source
    assert "_MSG_READ_PARTIAL" not in read_source


def test_read_video_decomposition_is_owned_by_video_adapter() -> None:
    builtin = PRODUCT_ROOT / "toolsets" / "builtin"
    read_source = (builtin / "read.py").read_text(encoding="utf-8")
    assert "TemporaryDirectory" not in read_source
    assert "_video_summary" not in read_source
    assert "def _clock(" not in read_source

    adapter = builtin / "read_adapters" / "video.py"
    adapter_tree = ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    assert {
        node.name
        for node in adapter_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {
        "VideoDecodeFailed",
        "VideoDecodeUnavailable",
        "_clock",
        "video_summary",
        "decompose_video_bytes",
    }


def test_code_map_collection_has_one_owner() -> None:
    code_map = PRODUCT_ROOT / "code_map"
    source = (code_map / "turn_context.py").read_text(encoding="utf-8")
    assert "def _collect_files(" not in source

    collection = code_map / "collection.py"
    tree = ast.parse(collection.read_text(encoding="utf-8"), filename=str(collection))
    assert {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {"_read_paths", "collect_code_map_files"}


def test_code_map_enrichment_has_one_owner() -> None:
    code_map = PRODUCT_ROOT / "code_map"
    source = (code_map / "turn_context.py").read_text(encoding="utf-8")
    assert "def _diff_shapes(" not in source
    assert "def _is_public_interface(" not in source
    assert "def _fill_unread_from_index(" not in source
    assert "def _fill_unread_symbols(" not in source
    assert "def _fill_precise_callers(" not in source
    assert "def _fill_surfaced_callers(" not in source
    assert "def _refresh_in_context(" not in source
    assert "def _has_content(" not in source
    assert "def _signature(" not in source

    enrichment = code_map / "enrichment.py"
    tree = ast.parse(enrichment.read_text(encoding="utf-8"), filename=str(enrichment))
    assert {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {
        "is_public_interface",
        "diff_symbol_shapes",
        "fill_unread_from_index",
        "fill_unread_symbols",
        "resolve_precise_callers",
        "resolve_in_context",
        "resolve_surfaced_callers",
        "neighborhood_has_content",
        "neighborhood_signature",
    }


def test_code_map_rendering_primitives_have_one_owner() -> None:
    code_map = PRODUCT_ROOT / "code_map"
    source = (code_map / "turn_context.py").read_text(encoding="utf-8")
    assert "def _render_unread(" not in source
    assert "def _render_symbols(" not in source
    assert "def _render_calls(" not in source
    assert "def _render_defines(" not in source
    assert "def _render_unread_used_by(" not in source
    assert "def _render_risk(" not in source
    assert "def _render_surfaced_callers(" not in source
    assert "def _render_file(" not in source
    assert "def _render_within_budget(" not in source
    assert "def _compose(" not in source

    rendering = code_map / "rendering.py"
    tree = ast.parse(rendering.read_text(encoding="utf-8"), filename=str(rendering))
    assert {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {
        "render_unread_target",
        "render_symbols",
        "render_calls",
        "render_defines",
        "render_unread_used_by",
        "render_interface_risk",
        "render_surfaced_callers",
        "render_file",
        "compose_code_map",
        "render_code_map",
    }


def test_view_projector_event_families_have_separate_handlers() -> None:
    projection = PRODUCT_ROOT / "presentation" / "projection"
    projector_source = (projection / "projector.py").read_text(encoding="utf-8")
    for event_name in (
        "OUTPUT_SNAPSHOT",
        "OUTPUT_SNAPSHOT_INVALIDATED",
        "OUTPUT_COMMITTED",
        "RUNTIME_DURABILITY_CHANGED",
        "TASK_PROGRESS",
        "ACTIVITY_STARTED",
        "ACTIVITY_COMPLETED",
        "BUDGET",
        "CONTEXT_COMPACTED",
        "MODEL_ATTEMPT_FINISHED",
        "LLM_STREAM_DELTA",
        "LLM_STREAM_COMMITTED",
        "LLM_STREAM_DISCARDED",
        "LLM_STREAM_INTERRUPTED",
        "LLM_STREAM_END",
        "MESSAGE_APPENDED",
    ):
        assert f"name == {event_name}" not in projector_source

    handlers = projection / "handlers"
    assert {path.name for path in handlers.glob("*.py")} >= {
        "__init__.py",
        "activity.py",
        "output.py",
        "system.py",
        "message.py",
        "tool_started.py",
        "tool_finished.py",
    }
    projector_tree = ast.parse(
        (projection / "projector.py").read_text(encoding="utf-8"),
        filename=str(projection / "projector.py"),
    )
    projector = next(
        node for node in projector_tree.body if isinstance(node, ast.ClassDef) and node.name == "ViewProjector"
    )
    assert {node.name for node in projector.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))} == {
        "__init__",
        "project",
        "_project",
    }
