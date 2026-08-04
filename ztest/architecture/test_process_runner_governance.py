from __future__ import annotations

import ast
from pathlib import Path

from mote.runtime import process
from mote.runtime.sandbox.config import SandboxProfile, SandboxRuntimeConfig
from mote.runtime.tools.permission.config import SandboxConfig

ROOT = Path(__file__).resolve().parents[2]


def test_one_shot_process_api_has_no_mixed_shell_switch() -> None:
    tree = ast.parse(Path(process.__file__).read_text(encoding="utf-8"))
    public_functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }

    assert set(public_functions) == {
        "resolve_fixed_executable",
        "run_verified_fixed_argv",
        "run_authorized_shell",
    }
    for node in public_functions.values():
        assert "shell" not in {argument.arg for argument in (*node.args.args, *node.args.kwonlyargs)}


def test_runtime_vcs_uses_fixed_argv_runner() -> None:
    source = (ROOT / "runtime/vcs/collector.py").read_text(encoding="utf-8")

    assert 'resolve_fixed_executable("git")' in source
    assert "create_subprocess_shell" not in source
    assert "aexecute" not in source


def test_production_has_no_legacy_generic_process_facade() -> None:
    offenders: list[str] = []
    for package in ("contracts", "runtime", "product"):
        for path in (ROOT / package).rglob("*.py"):
            if "aexecute" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_sandbox_profiles_are_closed_and_fail_closed() -> None:
    assert tuple(SandboxProfile) == (
        SandboxProfile.WORKSPACE_GOVERNED,
        SandboxProfile.NETWORKED_GOVERNED,
        SandboxProfile.ISOLATED_COMPUTE,
    )
    assert SandboxConfig().profile is SandboxProfile.WORKSPACE_GOVERNED
    assert SandboxRuntimeConfig().fail_if_unavailable is True
