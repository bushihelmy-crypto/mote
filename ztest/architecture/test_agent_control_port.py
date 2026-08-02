from __future__ import annotations

import ast
from pathlib import Path

from mote.contracts.ports.agent.control import AgentControlPort, ChildAgentHandlePort
from mote.runtime.agent import control

ROOT = Path(__file__).resolve().parents[2]


def test_ambient_control_uses_contracts_owned_port() -> None:
    annotations = control.__annotations__
    assert "AgentControlPort" in str(annotations["_ACTIVE_CONTROL"])
    assert "Any" not in str(annotations["_ACTIVE_CONTROL"])
    assert "SpawnPlan[OutputT]" in str(AgentControlPort.spawn_agent.__annotations__["spec"])
    assert "RunOutcome[OutputT]" in str(ChildAgentHandlePort.run_to_completion.__annotations__["return"])


def test_spawn_helper_preserves_output_generic_and_has_no_explicit_context() -> None:
    annotations = control.spawn_and_run.__annotations__
    assert "SpawnPlan[OutputT]" in str(annotations["spec"])
    assert "RunOutcome[OutputT]" in str(annotations["return"])
    signature = ast.parse((ROOT / "runtime/agent/control.py").read_text(encoding="utf-8"))
    helper = next(
        node for node in ast.walk(signature) if isinstance(node, ast.AsyncFunctionDef) and node.name == "spawn_and_run"
    )
    assert all(argument.arg != "ctx" for argument in (*helper.args.args, *helper.args.kwonlyargs))


def test_production_does_not_reflectively_discover_agent_control() -> None:
    violations: list[str] = []
    for package in ("contracts", "kernel", "runtime", "orchestration", "product"):
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id not in {"getattr", "hasattr"} or len(node.args) < 2:
                    continue
                name = node.args[1]
                if isinstance(name, ast.Constant) and name.value == "agent_control":
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_ambient_control_is_the_only_runtime_control_binding() -> None:
    role = (ROOT / "runtime/agent/role.py").read_text(encoding="utf-8")
    context = (ROOT / "runtime/agent/components/context.py").read_text(encoding="utf-8")
    runnable = (ROOT / "contracts/agent/spawn.py").read_text(encoding="utf-8")
    assert "self.agent_control" not in role
    assert "bind_agent_control" not in role
    assert "bind_agent_control" not in runnable
    assert "get_provider=resolve_control" in context
