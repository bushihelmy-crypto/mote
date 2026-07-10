#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.session.fork`` + ``Role.fork_session`` (Phase 4).

Covers: fork() seeds a new rollout from the parent's final history, records
parent_session_id + copied cwd/model anchors on session_meta, and stays
independent of the parent; missing source / existing target raise; replay
collapses a compaction checkpoint before seeding; Role.fork_session() returns a
sibling carrying the inherited history and lineage; listing surfaces the parent.
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import UserMessage
from metagpt.session.events import (
    CompactedEvent,
    MessageEvent,
    SessionMetaEvent,
)
from metagpt.session.fork import fork
from metagpt.session.history import diff_snapshot, file_history, restore
from metagpt.session.listing import list_sessions
from metagpt.session.log import SessionLog
from metagpt.session.replay import replay
from metagpt.session.snapshot import FileSnapshotRecorder


def _seed(tmp_path, sid, *, working_dir="/w", project_root="/p", model="m", messages=()):
    log = SessionLog(sid, base_dir=str(tmp_path))
    log.create(
        SessionMetaEvent(
            session_id=sid, working_dir=working_dir, project_root=project_root, model=model
        )
    )
    for content in messages:
        log.append(MessageEvent(message=UserMessage(content=content)))
    return log


def test_fork_seeds_history_and_lineage(tmp_path):
    _seed(tmp_path, "parent", working_dir="/repo", model="gpt-4", messages=["a", "b"])
    child_id = fork("parent", new_session_id="child", base_dir=str(tmp_path))
    assert child_id == "child"

    child = SessionLog("child", base_dir=str(tmp_path))
    records = list(child.iter_raw())
    assert records[0]["type"] == "session_meta"
    meta = records[0]["payload"]
    assert meta["session_id"] == "child"
    assert meta["parent_session_id"] == "parent"
    assert meta["working_dir"] == "/repo"
    assert meta["model"] == "gpt-4"
    # Inherited history replays to the parent's final state.
    result = replay(child)
    assert [m.content for m in result.messages] == ["a", "b"]


def test_fork_is_independent_of_parent(tmp_path):
    parent = _seed(tmp_path, "parent", messages=["a"])
    fork("parent", new_session_id="child", base_dir=str(tmp_path))
    # Mutating the child must not touch the parent's log.
    child = SessionLog("child", base_dir=str(tmp_path))
    child.append(MessageEvent(message=UserMessage(content="child-only")))
    parent_msgs = [r for r in parent.iter_raw() if r["type"] == "message"]
    assert [m["payload"]["content"] for m in parent_msgs] == ["a"]


def test_fork_generates_id_when_omitted(tmp_path):
    _seed(tmp_path, "parent", messages=["a"])
    child_id = fork("parent", base_dir=str(tmp_path))
    assert child_id and child_id != "parent"
    assert SessionLog(child_id, base_dir=str(tmp_path)).exists()


def test_fork_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        fork("nope", base_dir=str(tmp_path))


def test_fork_existing_target_raises(tmp_path):
    _seed(tmp_path, "parent", messages=["a"])
    _seed(tmp_path, "taken", messages=["x"])
    with pytest.raises(FileExistsError):
        fork("parent", new_session_id="taken", base_dir=str(tmp_path))


def test_fork_collapses_compaction_checkpoint(tmp_path):
    log = _seed(tmp_path, "parent", messages=["pre"])
    # A compaction resets history; later messages append after it.
    log.append(CompactedEvent(messages=[UserMessage(content="kept")], summary="s"))
    log.append(MessageEvent(message=UserMessage(content="after")))
    fork("parent", new_session_id="child", base_dir=str(tmp_path))
    result = replay(SessionLog("child", base_dir=str(tmp_path)))
    assert [m.content for m in result.messages] == ["kept", "after"]


def test_fork_listing_surfaces_parent(tmp_path):
    _seed(tmp_path, "parent", messages=["a"])
    fork("parent", new_session_id="child", base_dir=str(tmp_path))
    infos = {i.session_id: i for i in list_sessions(base_dir=str(tmp_path))}
    assert infos["child"].parent_session_id == "parent"
    assert infos["parent"].parent_session_id is None


def test_fork_inherits_file_history_and_blobs(tmp_path):
    log = _seed(tmp_path, "parent", messages=["a"])
    target = tmp_path / "f.txt"
    target.write_text("v1")
    rec = FileSnapshotRecorder(log)
    rec.snapshot(str(target), tool="Write")
    target.write_text("v2")  # current on-disk now differs from the before-image

    fork("parent", new_session_id="child", base_dir=str(tmp_path))
    child = SessionLog("child", base_dir=str(tmp_path))

    # The child sees the inherited snapshot event...
    hist = file_history(child)
    assert str(target) in hist
    assert hist[str(target)][0].pre_hash is not None
    # ...and can diff/restore using its own (copied) blob store.
    diff = diff_snapshot(child, str(target))
    assert "v1" in diff and "v2" in diff
    assert restore(child, str(target)) is True
    assert target.read_text() == "v1"


def test_fork_file_history_independent_of_parent(tmp_path):
    log = _seed(tmp_path, "parent", messages=["a"])
    target = tmp_path / "f.txt"
    target.write_text("v1")
    FileSnapshotRecorder(log).snapshot(str(target), tool="Write")

    fork("parent", new_session_id="child", base_dir=str(tmp_path))
    # The child's blob store is its own dir, not shared with the parent.
    child_blobs = tmp_path / "child" / "blobs"
    assert child_blobs.exists() and any(child_blobs.rglob("*"))


@pytest.mark.asyncio
async def test_role_fork_session_inherits_history_and_lineage(tmp_path, monkeypatch):
    from metagpt.roles import Role
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)

    parent = Role(name="P", context=Context())
    parent._components._wire_spine()  # wire the recorder subscriber
    await parent.context_manager.add(UserMessage(content="one"))
    await parent.context_manager.add(UserMessage(content="two"))

    child = parent.fork_session()
    assert child.state.parent_session_id == parent.session_id
    assert child.session_id != parent.session_id
    assert child.state.recovered is True
    assert [m.content for m in child.context_manager.get()] == ["one", "two"]
