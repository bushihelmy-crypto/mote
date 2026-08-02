"""Static ownership gates for the canonical Product Application factory."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "product/composition/bootstrap.py"
CLI_BOOTSTRAP = ROOT / "product/entrypoints/cli/bootstrap.py"
CLI_MAIN = ROOT / "product/entrypoints/cli/__main__.py"


def _application_constructor_calls(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "mote.product.composition.application"
        for alias in node.names
        if alias.name == "Application"
    }
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in aliases
    )


def test_product_application_has_one_constructor_owner() -> None:
    callsites = {
        path.relative_to(ROOT).as_posix(): _application_constructor_calls(path)
        for path in (ROOT / "product").rglob("*.py")
        if _application_constructor_calls(path)
    }
    assert callsites == {"product/composition/bootstrap.py": 1}


def test_cli_owns_only_the_outer_event_loop_boundary() -> None:
    composition_source = BOOTSTRAP.read_text(encoding="utf-8")
    cli_source = CLI_BOOTSTRAP.read_text(encoding="utf-8")
    main_source = CLI_MAIN.read_text(encoding="utf-8")
    assert "asyncio.run(" not in composition_source
    assert "asyncio.run(" not in cli_source
    assert main_source.count("asyncio.run(") == 1
    assert "ProductContainer.standard(" not in cli_source
    assert "EngineServices(" not in cli_source


def test_trusted_workflow_blueprints_activate_only_at_composition_root() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "workflow_blueprints: tuple[TrustedWorkflowBlueprint, ...]" in source
    assert "for blueprint in request.workflow_blueprints:" in source
    assert "workflow_durability.register_trusted_blueprint(" in source
