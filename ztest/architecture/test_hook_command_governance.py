from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mote.contracts.hook import HookIdentity, StopInvocation, StopPayload
from mote.runtime.config.hook import HookCommandHandler, HookConfig, HookMatcherGroup
from mote.runtime.hook.command_handler import HookCommandFailure, run_command_handler
from mote.runtime.hook.manager import HookManager
from mote.runtime.hook.parser import parse_command_output
from mote.runtime.process import ProcessDisposition, ProcessResult


def test_hook_commands_are_structured_absolute_argv() -> None:
    with pytest.raises(ValidationError):
        HookCommandHandler.model_validate({"command": "echo unsafe"})
    with pytest.raises(ValidationError):
        HookCommandHandler(id="unsafe", argv=("echo", "unsafe"))
    assert HookCommandHandler(id="safe", argv=("/bin/echo", "safe")).argv == (
        "/bin/echo",
        "safe",
    )
    with pytest.raises(ValidationError):
        HookConfig(
            events={
                "PreToolUse": [
                    HookMatcherGroup(
                        handlers=[
                            HookCommandHandler(id="duplicate", argv=("/bin/true",)),
                            HookCommandHandler(id="duplicate", argv=("/bin/true",)),
                        ]
                    )
                ]
            }
        )


def test_hook_command_path_has_no_shell_or_parent_environment() -> None:
    source = Path("runtime/hook/command_handler.py").read_text(encoding="utf-8")
    assert "create_subprocess_shell" not in source
    assert "dict(os.environ)" not in source
    assert "AGENT_SESSION_ID" not in source
    assert "AGENT_PROJECT_DIR" not in source
    assert "max_output_bytes=MAX_HOOK_OUTPUT_BYTES" in source
    assert "sandbox.wrap_exec(" in source

    manager = Path("runtime/hook/manager.py").read_text(encoding="utf-8")
    assert 'engine.check(\n                "HookCommand"' in manager
    assert "HookAuthorizationFact(cfg.id" in manager

    composition = Path("runtime/agent/components/integrations.py").read_text(encoding="utf-8")
    assert "permission_engine=ctx.dep(PERMISSION_ENGINE)" in composition
    assert "command_sandbox=ctx.dep(SANDBOX_RUNTIME)" in composition

    policy = Path("runtime/tools/policy.py").read_text(encoding="utf-8")
    assert 'step=f"hook_command:{fact.handler_id}"' in policy


@pytest.mark.asyncio
async def test_control_callback_failure_is_explicit_deny() -> None:
    manager = HookManager()

    def fail(_invocation: object) -> None:
        raise RuntimeError("secret must not be logged")

    manager.register("PreToolUse", fail)
    outcome = await manager._run_callback("PreToolUse", fail, object())  # type: ignore[arg-type]
    assert outcome.behavior == "deny"
    assert outcome.system_message == "control hook failed closed"


@pytest.mark.asyncio
async def test_observation_callback_failure_is_best_effort() -> None:
    manager = HookManager()

    def fail(_invocation: object) -> None:
        raise RuntimeError("secret must not be logged")

    outcome = await manager._run_callback("PostToolUse", fail, object())  # type: ignore[arg-type]
    assert outcome.behavior is None


def test_control_wire_rejects_malformed_and_unknown_decisions() -> None:
    with pytest.raises(ValueError):
        parse_command_output("not-json", "", 0, strict=True)
    with pytest.raises(ValueError):
        parse_command_output('{"decision":"unknown"}', "", 0, strict=True)
    with pytest.raises(ValueError):
        parse_command_output(
            '{"hookSpecificOutput":{"permissionDecision":"unknown"}}',
            "",
            0,
            strict=True,
        )


class _Sandbox:
    async def wrap_exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        extra_writable: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        return ["/sandbox", *argv], dict(env or {})


@pytest.mark.asyncio
async def test_command_runner_uses_bounded_stdin_and_sandbox(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fixed(argv, **kwargs):
        captured["argv"] = tuple(argv)
        captured.update(kwargs)
        return ProcessResult(
            ProcessDisposition.EXITED,
            stdout='{"decision":"approve"}',
            exit_code=0,
        )

    import mote.runtime.hook.command_handler as command_handler

    monkeypatch.setattr(command_handler, "run_fixed_argv", fixed)
    outcome = await run_command_handler(
        HookCommandHandler(id="bounded", argv=("/bin/true",)),
        StopInvocation(HookIdentity(), StopPayload()),
        sandbox=_Sandbox(),
    )
    assert outcome.behavior == "allow"
    assert captured["argv"] == ("/sandbox", "/bin/true")
    assert captured["max_output_bytes"] == 256 * 1024
    assert isinstance(captured["stdin"], bytes)
    assert "SECRET" not in captured["env"]


@pytest.mark.asyncio
async def test_command_timeout_is_typed_failure(monkeypatch) -> None:
    async def fixed(argv, **kwargs):
        return ProcessResult(ProcessDisposition.TIMED_OUT)

    import mote.runtime.hook.command_handler as command_handler

    monkeypatch.setattr(command_handler, "run_fixed_argv", fixed)
    with pytest.raises(HookCommandFailure):
        await run_command_handler(
            HookCommandHandler(id="timeout", argv=("/bin/true",)),
            StopInvocation(HookIdentity(), StopPayload()),
            sandbox=_Sandbox(),
        )
