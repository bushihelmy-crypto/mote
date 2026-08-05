"""Final Runtime package-governance gates."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_LEGACY_PATHS = (
    "mote.runtime.paths",
    "mote.runtime.maintenance",
    "mote.orchestration.background_tasks.role_component",
)
FORBIDDEN_PATH_FILENAMES = (
    "runtime/config/locations.py",
    "runtime/config/discovery.py",
    "runtime/defaults.py",
    "runtime/locations.py",
    "runtime/discovery.py",
)
FORBIDDEN_SERIALIZATION_IMPORTS = {"pickle", "dill", "cloudpickle"}
FORBIDDEN_INFERENCE_TRANSPORT_IMPORTS = {"grpc"}


def _imports(path: Path) -> list[tuple[int, str]]:
    modules: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.relative_to(PACKAGE_ROOT).with_suffix("").parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = "." * node.level + (node.module or "")
                module = importlib.util.resolve_name(prefix, f"mote.{package}")
            else:
                module = node.module or ""
            modules.append((node.lineno, module))
    return modules


def _string_constants(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _dynamic_import_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
            ):
                calls.append((node.lineno, "importlib.import_module"))
        elif isinstance(node.func, ast.Name) and node.func.id == "__import__":
            calls.append((node.lineno, "__import__"))
    return calls


def test_removed_runtime_and_task_adapter_entries_stay_deleted() -> None:
    removed = (
        "runtime/paths.py",
        "runtime/workspace",
        "runtime/disk",
        "runtime/maintenance.py",
        "runtime/reconciliation.py",
        "runtime/completion",
        "runtime/logging",
        "runtime/observability",
        "runtime/scheduling",
        "orchestration/tasks/role_component.py",
    )
    assert not [entry for entry in removed if (PACKAGE_ROOT / entry).exists()]


def test_removed_imports_and_task_boundaries() -> None:
    violations: list[str] = []
    for layer in ("runtime", "orchestration", "product"):
        for path in (PACKAGE_ROOT / layer).rglob("*.py"):
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            for lineno, module in _imports(path):
                if module.startswith(FORBIDDEN_LEGACY_PATHS):
                    violations.append(f"{relative}:{lineno}: {module}")
                if layer == "runtime" and module.startswith("mote.orchestration.background_tasks"):
                    violations.append(f"{relative}:{lineno}: runtime -> {module}")
                if relative.startswith("orchestration/tasks/") and module.startswith("mote.runtime.agent"):
                    violations.append(f"{relative}:{lineno}: tasks -> {module}")
    assert not violations, "Runtime governance import violations:\n" + "\n".join(violations)


def test_runtime_governance_rejects_legacy_path_strings_and_dynamic_compat_shims() -> None:
    violations: list[str] = []
    for layer in ("runtime", "orchestration", "product"):
        for path in (PACKAGE_ROOT / layer).rglob("*.py"):
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            source_constants = _string_constants(path)
            for lineno, value in source_constants:
                if value in FORBIDDEN_LEGACY_PATHS:
                    violations.append(f"{relative}:{lineno}: {value}")
            for lineno, call_name in _dynamic_import_calls(path):
                for _, value in source_constants:
                    if value in FORBIDDEN_LEGACY_PATHS:
                        violations.append(f"{relative}:{lineno}: {call_name} -> {value}")
    assert not violations, "Legacy path strings are forbidden:\n" + "\n".join(violations)


def test_runtime_governance_rejects_pickle_module_dependencies() -> None:
    violations: list[str] = []
    for layer in ("runtime", "orchestration", "product"):
        for path in (PACKAGE_ROOT / layer).rglob("*.py"):
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            for lineno, module in _imports(path):
                if module.split(".")[0] in FORBIDDEN_SERIALIZATION_IMPORTS:
                    violations.append(f"{relative}:{lineno}: import {module}")
    assert not violations, "Pickle-style module dependencies are forbidden:\n" + "\n".join(violations)


def test_transport_neutral_layers_do_not_depend_on_grpc() -> None:
    violations: list[str] = []
    for layer in ("contracts", "kernel", "runtime", "orchestration"):
        for path in (PACKAGE_ROOT / layer).rglob("*.py"):
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            for lineno, module in _imports(path):
                if module.split(".")[0] in FORBIDDEN_INFERENCE_TRANSPORT_IMPORTS:
                    violations.append(f"{relative}:{lineno}: import {module}")
    assert not violations, "gRPC is a Product inference adapter only:\n" + "\n".join(violations)


def test_background_task_adapter_does_not_reach_role_internals() -> None:
    paths = [PACKAGE_ROOT / "product/agents/background_tasks.py"]
    paths.extend((PACKAGE_ROOT / "orchestration/tasks").rglob("*.py"))
    forbidden = {"ctx.role", "role._capabilities", "role.resource_registry"}
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                rendered = ast.unparse(node)
                if rendered in forbidden:
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}: {rendered}")
    assert not violations, "Task adapter reaches Role internals:\n" + "\n".join(violations)


def test_governance_public_surfaces_are_owner_scoped() -> None:
    import mote.contracts.ports.task.operations as background_ports
    import mote.contracts.task.models as background_contracts
    import mote.orchestration.background_tasks as tasks
    import mote.product.agents.background_tasks as background_tasks

    assert background_tasks.__all__ == ["build_background_task_pool"]
    assert not hasattr(tasks, "build_background_task_pool")
    assert background_contracts.__all__ == [
        "CommandName",
        "AttemptId",
        "CompletedInlineTaskResultPointer",
        "CompletedArtifactTaskResultPointer",
        "FailedTaskResultPointer",
        "InlineTaskOutput",
        "TaskFailure",
        "TaskId",
        "TaskResultPointer",
        "TaskResultRecord",
    ]
    assert background_ports.__all__ == [
        "AgentWakePort",
        "BackgroundMessageSink",
        "BackgroundTaskBuildContext",
        "BackgroundTaskService",
        "BackgroundTaskServiceFactory",
        "BackgroundTaskSnapshot",
        "LocalAsyncWorkAdapter",
        "TaskOutputLocationPort",
        "TaskResultRegistry",
    ]
    assert not (PACKAGE_ROOT / "contracts/ports/config_source_provider.py").exists()


def test_runtime_path_policy_modules_are_absent() -> None:
    assert not [entry for entry in FORBIDDEN_PATH_FILENAMES if (PACKAGE_ROOT / entry).exists()]


def test_runtime_does_not_own_product_home_defaults_or_discovery() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "runtime").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if "Path.home()" in source and relative != ("runtime/interactive/canvas/backends/drawio.py"):
            violations.append(f"{path.relative_to(PACKAGE_ROOT)}: Path.home()")
    assert not violations, "Runtime owns Product path policy:\n" + "\n".join(violations)
