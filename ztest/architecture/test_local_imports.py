"""Production function-local imports are a frozen, shrinking migration queue."""
from __future__ import annotations

import ast
from collections import Counter
from enum import Enum
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# Existing lazy/optional/platform/cycle boundaries. Counts distinguish repeated
# imports in one module. New entries fail; resolved entries must be deleted.
MIGRATION_BASELINE: dict[tuple[str, str], int] = {
    ("cli/__init__.py", "mote.cli.app"): 1,
    ("cli/__main__.py", "mote.cli.app"): 1,
    ("cli/__main__.py", "mote.cli.consumers.acp.server"): 1,
    ("cli/__main__.py", "mote.cli.consumers.agui.server"): 1,
    ("cli/__main__.py", "mote.cli.consumers.textual"): 1,
    ("cli/__main__.py", "mote.cli.consumers.textual.bootstrap"): 1,
    ("cli/__main__.py", "mote.cli.cron_cli"): 1,
    ("cli/commands/registry.py", "mote.cli.commands.builtin"): 1,
    ("cli/consumers/render/mathbox.py", "pylatexenc.latex2text"): 1,
    ("cli/consumers/render/mathbox.py", "pylatexenc.latexwalker"): 4,
    ("cli/consumers/textual/__init__.py", "importlib"): 1,
    ("cli/consumers/textual/style.py", "textual.theme"): 1,
    ("common/observability/langfuse_backend.py", "langfuse"): 1,
    ("common/observability/langfuse_integration.py", "langfuse"): 1,
    ("common/text/html.py", "markdownify"): 1,
    ("durable_exec/temporal/_backend.py", "temporalio"): 1,
    ("durable_exec/temporal/_backend.py", "temporalio.common"): 1,
    ("durable_exec/temporal/plugin.py", "temporalio.client"): 1,
    ("durable_exec/temporal/plugin.py", "temporalio.worker"): 1,
    ("executor/compress/curl.py", "bs4"): 1,
    ("executor/dependency/_browser.py", "playwright.async_api"): 1,
    ("executor/dependency/_kernel.py", "jupyter_client.manager"): 1,
    ("executor/dependency/_terminal.py", "pty"): 1,
    ("executor/dependency/_terminal.py", "termios"): 1,
    ("executor/tools/generate_media/creators.py", "openai"): 1,
    ("executor/tools/read.py", "PIL"): 1,
    ("router/llm/anthropic_api.py", "anthropic"): 1,
    ("router/llm/anthropic_api.py", "httpx"): 1,
    ("router/llm/openai_responses_api.py", "openai"): 1,
    ("router/llm/openai_responses_api.py", "openai._base_client"): 1,
    ("router/llm/openai_responses_api.py", "openai.types.responses"): 1,
    ("router/llm/transformers.py", "PIL"): 1,
    ("router/ml/bge_onnx.py", "onnxruntime"): 1,
    ("router/ml/bge_onnx.py", "tokenizers"): 1,
    ("router/ml/inference/artifacts.py", "joblib"): 1,
    ("router/ml/inference/artifacts.py", "lightgbm"): 1,
    ("router/ml/inference/artifacts.py", "onnxruntime"): 1,
    ("router/ml/v4_features.py", "sentence_transformers"): 1,
    ("router/oauth/manager.py", "filelock"): 1,
    ("router/oauth/storage/keyring_store.py", "keyring"): 1,
    ("sandbox/network/tls.py", "certifi"): 1,
    ("sandbox/runtime.py", "mote.sandbox.network.tls"): 1,
    ("sandbox/seccomp.py", "pyseccomp"): 3,
}


class BoundaryReason(str, Enum):
    OPTIONAL_DEPENDENCY = "optional_dependency"
    PLATFORM = "platform"
    PLUGIN_DISCOVERY = "plugin_discovery"
    LAZY_BOOTSTRAP = "lazy_bootstrap"


_PLATFORM_IMPORTS = {"pty", "termios", "pyseccomp"}
_PLUGIN_DISCOVERY = {
    ("cli/commands/registry.py", "mote.cli.commands.builtin"),
    ("cli/consumers/textual/__init__.py", "importlib"),
}
_LAZY_BOOTSTRAP = {
    ("cli/__init__.py", "mote.cli.app"),
    ("cli/__main__.py", "mote.cli.app"),
    ("cli/__main__.py", "mote.cli.consumers.acp.server"),
    ("cli/__main__.py", "mote.cli.consumers.agui.server"),
    ("cli/__main__.py", "mote.cli.consumers.textual"),
    ("cli/__main__.py", "mote.cli.consumers.textual.bootstrap"),
    ("cli/__main__.py", "mote.cli.cron_cli"),
    ("sandbox/runtime.py", "mote.sandbox.network.tls"),
}


def _boundary_reason(key: tuple[str, str]) -> BoundaryReason:
    if key in _PLUGIN_DISCOVERY:
        return BoundaryReason.PLUGIN_DISCOVERY
    if key in _LAZY_BOOTSTRAP:
        return BoundaryReason.LAZY_BOOTSTRAP
    module = key[1]
    if module.split(".", 1)[0] in _PLATFORM_IMPORTS:
        return BoundaryReason.PLATFORM
    if not module.startswith("mote."):
        return BoundaryReason.OPTIONAL_DEPENDENCY
    raise AssertionError(f"Unclassified internal local-import boundary: {key}")


class _LocalImportCounter(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.function_depth = 0
        self.imports: Counter[tuple[str, str]] = Counter()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        if self.function_depth:
            for alias in node.names:
                self.imports[(self.path, alias.name)] += 1

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.function_depth and node.module:
            self.imports[(self.path, node.module)] += 1


def _production_local_imports() -> Counter[tuple[str, str]]:
    imports: Counter[tuple[str, str]] = Counter()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "ztest" in path.parts or ".venv" in path.parts:
            continue
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        visitor = _LocalImportCounter(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        imports.update(visitor.imports)
    return imports


def test_production_local_imports_only_shrink() -> None:
    current = _production_local_imports()
    baseline = Counter(MIGRATION_BASELINE)
    added = current - baseline
    removed = baseline - current

    assert not removed, "Delete resolved local imports from MIGRATION_BASELINE:\n" + "\n".join(
        f"{key}: {count}" for key, count in sorted(removed.items())
    )
    assert not added, "New production function-local imports are forbidden:\n" + "\n".join(
        f"{key}: {count}" for key, count in sorted(added.items())
    )


def test_local_import_baseline_has_machine_readable_reasons() -> None:
    reasons = {_boundary_reason(key) for key in MIGRATION_BASELINE}
    assert reasons == set(BoundaryReason)
