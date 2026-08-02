from __future__ import annotations

import ast
from pathlib import Path

from mote.runtime import process

ROOT = Path(__file__).resolve().parents[2]


def test_one_shot_process_api_has_no_mixed_shell_switch() -> None:
    tree = ast.parse(Path(process.__file__).read_text(encoding="utf-8"))
    public_functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }

    assert set(public_functions) == {
        "run_fixed_argv",
        "run_verified_fixed_argv",
        "run_authorized_shell",
    }
    for node in public_functions.values():
        assert "shell" not in {argument.arg for argument in (*node.args.args, *node.args.kwonlyargs)}


def test_runtime_vcs_uses_fixed_argv_runner() -> None:
    source = (ROOT / "runtime/vcs/collector.py").read_text(encoding="utf-8")

    assert 'run_fixed_argv(("git", *args)' in source
    assert "create_subprocess_shell" not in source
    assert "aexecute" not in source


def test_production_has_no_legacy_generic_process_facade() -> None:
    offenders: list[str] = []
    for package in ("contracts", "runtime", "product"):
        for path in (ROOT / package).rglob("*.py"):
            if "aexecute" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
