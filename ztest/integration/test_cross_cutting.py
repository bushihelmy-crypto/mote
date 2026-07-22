#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end cross-cutting concerns during a real ``Role.run``.

Verifies that the opt-in layers — lifecycle hooks, the permission engine, and
file-history snapshots — engage correctly when wired through a real Role
driving real tools via a scripted LLM:

* PreToolUse / PostToolUse hooks fire around real tool calls (and a PreToolUse
  ``block`` denies the call, composing with the permission engine's deny-wins).
* a ``deny`` permission rule blocks a tool before it touches the disk.
* file-mutating tools record a before-image into the session blob store, and
  ``session.history.restore`` rolls the file back.
"""
from __future__ import annotations

import os

import pytest

from mote.common.schema import PermissionConfig

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


async def test_pre_and_post_tool_use_hooks_fire(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "hooked.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "hi"})], "done"],
    )

    seen: list[tuple[str, str]] = []

    async def pre(hook_input):
        seen.append(("pre", hook_input.payload.get("tool_name", "")))
        return None  # allow

    async def post(hook_input):
        seen.append(("post", hook_input.payload.get("tool_name", "")))
        return None

    role.register_hook("PreToolUse", pre)
    role.register_hook("PostToolUse", post)

    await role.run(with_message="write hooked.txt")

    assert ("pre", "Edit") in seen
    assert ("post", "Edit") in seen
    # The tool still ran (hooks observed, did not block).
    assert os.path.exists(target)


async def test_lifecycle_hooks_fire_in_order(make_role, tmp_path):
    """SessionStart fires once at the top, Stop once at the end of a run."""
    target = os.path.join(str(tmp_path), "lc.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "x"})], "done"],
    )

    events: list[str] = []

    async def on_start(hook_input):
        events.append("SessionStart")

    async def on_stop(hook_input):
        events.append("Stop")

    role.register_hook("SessionStart", on_start)
    role.register_hook("Stop", on_stop)

    await role.run(with_message="go")

    # SessionStart precedes Stop, each fired exactly once.
    assert events == ["SessionStart", "Stop"]


async def test_user_prompt_submit_injects_context(make_role, tmp_path):
    """A UserPromptSubmit hook's additionalContext lands in the conversation."""
    target = os.path.join(str(tmp_path), "ctx.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "x"})], "done"],
    )

    async def add_context(hook_input):
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "INJECTED-CONTEXT",
            }
        }

    role.register_hook("UserPromptSubmit", add_context)

    await role.run(with_message="go")

    contents = [m.content for m in role.context_manager.get()]
    assert any("INJECTED-CONTEXT" in c for c in contents)


async def test_post_tool_use_appends_additional_context(make_role, tmp_path):
    """A PostToolUse hook's additionalContext is appended to the tool output."""
    target = os.path.join(str(tmp_path), "ap.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "x"})], "done"],
    )

    async def annotate(hook_input):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "POST-ANNOTATION",
            }
        }

    role.register_hook("PostToolUse", annotate, matcher="Edit")

    await role.run(with_message="go")

    contents = [m.content for m in role.context_manager.get()]
    assert any("POST-ANNOTATION" in c for c in contents)


async def test_pre_tool_use_hook_rewrites_args(make_role, tmp_path):
    """A PreToolUse hook can rewrite the tool's input before it runs."""
    target = os.path.join(str(tmp_path), "rw.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "ORIGINAL"})], "done"],
    )

    async def rewrite(hook_input):
        args = dict(hook_input.payload.get("tool_input") or {})
        args["new_string"] = "REWRITTEN"
        return {
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"},
            "updatedInput": args,
        }

    role.register_hook("PreToolUse", rewrite, matcher="Edit")

    await role.run(with_message="write rw.txt")

    # The tool ran with the hook's rewritten content, not the model's original.
    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert f.read() == "REWRITTEN"


async def test_pre_tool_use_block_denies_tool(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "blocked.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "should not exist"})], "done"],
    )

    async def deny(hook_input):
        return {"decision": "block", "reason": "nope"}

    role.register_hook("PreToolUse", deny, matcher="Edit")

    await role.run(with_message="try to write")

    # The PreToolUse block prevented the write from ever hitting the disk.
    assert not os.path.exists(target)


# ---------------------------------------------------------------------------
# Permission engine
# ---------------------------------------------------------------------------


async def test_permission_deny_blocks_tool(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "denied.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        permissions=PermissionConfig(deny=["Edit"]),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "blocked by policy"})], "done"],
    )

    await role.run(with_message="write denied.txt")

    assert not os.path.exists(target)
    # The denied tool-result is recorded in history.
    contents = [m.content for m in role.context_manager.get()]
    assert any('code="TOOL_PERMISSION_DENIED"' in c for c in contents)


async def test_permission_allow_lets_tool_run(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "allowed.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        permissions=PermissionConfig(allow=["Edit"], mode="default"),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "ok"})], "done"],
    )

    await role.run(with_message="write allowed.txt")

    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert f.read() == "ok"


# ---------------------------------------------------------------------------
# File-history snapshots
# ---------------------------------------------------------------------------


async def test_file_snapshot_captured_and_restorable(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "doc.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("original\n")

    role = make_role(
        working_dir=str(tmp_path),
        tools=["Read", "Edit"],
        snapshot_backend="blob",  # deterministic: no dependency on a git binary
        turns=[
            [("Read", {"file_path": target})],
            [("Edit", {"file_path": target, "old_string": "original", "new_string": "modified"})],
            "done",
        ],
    )

    await role.run(with_message="modify doc.txt")

    # The edit landed.
    with open(target, encoding="utf-8") as f:
        assert "modified" in f.read()

    from mote.session import SessionLog
    from mote.session.history import file_history, restore

    log = SessionLog(role.state.session_id)
    history = file_history(log)
    # A before-image was recorded for the edited file.
    assert any(os.path.basename(p) == "doc.txt" for p in history)

    # Restoring the before-image rolls the file back to its original content.
    snap_path = next(p for p in history if os.path.basename(p) == "doc.txt")
    assert restore(log, snap_path) is True
    with open(target, encoding="utf-8") as f:
        assert f.read() == "original\n"


async def test_multiple_edits_record_indexed_snapshots(make_role, tmp_path):
    """Two edits to one file record two ordered before-images; restore/diff by index."""
    target = os.path.join(str(tmp_path), "doc.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("v0\n")

    role = make_role(
        working_dir=str(tmp_path),
        tools=["Read", "Edit"],
        snapshot_backend="blob",
        turns=[
            [("Read", {"file_path": target})],
            [("Edit", {"file_path": target, "old_string": "v0", "new_string": "v1"})],
            [("Edit", {"file_path": target, "old_string": "v1", "new_string": "v2"})],
            "done",
        ],
    )

    await role.run(with_message="edit doc twice")

    with open(target, encoding="utf-8") as f:
        assert f.read().strip() == "v2"

    from mote.session import SessionLog
    from mote.session.history import diff_snapshot, file_history, restore

    log = SessionLog(role.state.session_id)
    history = file_history(log)
    key = next(p for p in history if os.path.basename(p) == "doc.txt")
    entries = history[key]
    # Two before-images, ordered 0,1.
    assert [e.index for e in entries] == [0, 1]

    # The latest before-image was "v1"; the diff against the on-disk "v2" shows it.
    last_diff = diff_snapshot(log, key, index=-1)
    assert "v1" in last_diff and "v2" in last_diff

    # Restoring the *first* before-image rolls all the way back to the original.
    assert restore(log, key, index=0) is True
    with open(target, encoding="utf-8") as f:
        assert f.read().strip() == "v0"


async def test_snapshot_of_created_file_restores_by_deleting(make_role, tmp_path):
    """A create snapshot has no before-image; restoring it removes the file."""
    target = os.path.join(str(tmp_path), "created.txt")  # does not exist yet
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        snapshot_backend="blob",
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "brand new"})], "done"],
    )

    await role.run(with_message="create created.txt")

    assert os.path.exists(target)

    from mote.session import SessionLog
    from mote.session.history import file_history, restore

    log = SessionLog(role.state.session_id)
    history = file_history(log)
    snap_path = next(p for p in history if os.path.basename(p) == "created.txt")

    # The before-image was "file did not exist", so restoring deletes the file.
    assert restore(log, snap_path) is True
    assert not os.path.exists(target)
