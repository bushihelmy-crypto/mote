#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for HookManager.fire: callbacks, command handlers, folding, isolation."""
from __future__ import annotations

import json
import os
import stat

import pytest

from mote.common.hook.manager import HookManager
from mote.common.hook.types import HookOutcome
from mote.common.schema import HookCommandHandler, HookConfig, HookMatcherGroup


@pytest.mark.asyncio
async def test_no_handlers_returns_empty_fast_path():
    mgr = HookManager()
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.behavior is None
    assert mgr.enabled is False


@pytest.mark.asyncio
async def test_callback_sync_dict():
    mgr = HookManager()
    mgr.register("UserPromptSubmit", lambda hi: {"additionalContext": "hi"})
    out = await mgr.fire("UserPromptSubmit", {"prompt": "x"})
    assert out.additional_context == ["hi"]
    assert mgr.enabled is True


@pytest.mark.asyncio
async def test_callback_async_outcome():
    mgr = HookManager()

    async def cb(hi):
        return HookOutcome(behavior="deny", system_message="blocked")

    mgr.register("PreToolUse", cb)
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.behavior == "deny"
    assert out.system_message == "blocked"


@pytest.mark.asyncio
async def test_callback_matcher_filters():
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "block"}, matcher="Write")
    # Tool name Bash does not match the Write matcher -> no handler runs.
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.behavior is None
    out2 = await mgr.fire("PreToolUse", {"tool_name": "Write"})
    assert out2.behavior == "deny"


@pytest.mark.asyncio
async def test_failure_isolation():
    mgr = HookManager()

    def boom(hi):
        raise RuntimeError("nope")

    mgr.register("PreToolUse", boom)
    mgr.register("PreToolUse", lambda hi: {"additionalContext": "still here"})
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    # The throwing handler is skipped; the good one still contributes.
    assert out.additional_context == ["still here"]


@pytest.mark.asyncio
async def test_fold_deny_wins_across_handlers():
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "approve"})
    mgr.register("PreToolUse", lambda hi: {"decision": "block"})
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.behavior == "deny"


def _write_script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR | stat.S_IWUSR)
    return str(path)


@pytest.mark.asyncio
async def test_command_handler_json_stdout(tmp_path):
    # A script that emits a JSON block decision on stdout.
    script = _write_script(
        tmp_path,
        "block.sh",
        "#!/usr/bin/env bash\nread -r line\n"
        'echo \'{"hookSpecificOutput": {"permissionDecision": "deny"}, "systemMessage": "no"}\'\n',
    )
    cfg = HookConfig(events={"PreToolUse": [HookMatcherGroup(handlers=[HookCommandHandler(command=f"bash {script}")])]})
    mgr = HookManager(cfg, session_id="sid", get_cwd=lambda: str(tmp_path))
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.behavior == "deny"
    assert out.system_message == "no"


@pytest.mark.asyncio
async def test_command_handler_exit_2_blocks(tmp_path):
    script = _write_script(
        tmp_path,
        "deny.sh",
        "#!/usr/bin/env bash\nread -r line\necho 'denied via exit' >&2\nexit 2\n",
    )
    cfg = HookConfig(events={"PreToolUse": [HookMatcherGroup(handlers=[HookCommandHandler(command=f"bash {script}")])]})
    mgr = HookManager(cfg)
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.behavior == "deny"
    assert "denied via exit" in out.system_message


@pytest.mark.asyncio
async def test_command_handler_receives_payload_on_stdin(tmp_path):
    # Echo back the toolName field read from stdin as additionalContext.
    script = _write_script(
        tmp_path,
        "echo.sh",
        "#!/usr/bin/env bash\nread -r line\n"
        'name=$(python3 -c \'import sys,json; print(json.load(sys.stdin)["tool_name"])\' <<< "$line")\n'
        'echo "{\\"additionalContext\\": \\"$name\\"}"\n',
    )
    cfg = HookConfig(events={"PreToolUse": [HookMatcherGroup(handlers=[HookCommandHandler(command=f"bash {script}")])]})
    mgr = HookManager(cfg)
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.additional_context == ["Bash"]


@pytest.mark.asyncio
async def test_command_handler_matcher_group_filters(tmp_path):
    script = _write_script(
        tmp_path,
        "block.sh",
        "#!/usr/bin/env bash\nread -r line\nexit 2\n",
    )
    cfg = HookConfig(
        events={
            "PreToolUse": [HookMatcherGroup(matcher="Write", handlers=[HookCommandHandler(command=f"bash {script}")])]
        }
    )
    mgr = HookManager(cfg)
    # Bash does not match the Write group -> passthrough.
    out = await mgr.fire("PreToolUse", {"tool_name": "Bash"})
    assert out.behavior is None
