#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for transaction-backed file history."""
from __future__ import annotations

import asyncio
import hashlib

from mote.contracts.fileops import MutationSet, RecoveryPolicy, ReplaceMutation
from mote.product.toolsets.builtin.edit import Edit
from mote.runtime.fileops import FileOperations
from mote.runtime.fileops.transactions import ScopedMutationArtifacts
from mote.runtime.session.codec import iter_file_operations_events
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.history import file_history
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay

# ---------------------------------------------------------------------------
# replay ignores snapshot events
# ---------------------------------------------------------------------------


def test_replay_ignores_file_transaction_events(tmp_path):
    from mote.contracts.schema import UserMessage
    from mote.runtime.session.events import MessageEvent

    log = SessionLog("mix", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent(session_id="mix"))
    log.commit_offline(MessageEvent(message=UserMessage(content="hi")))
    target = tmp_path / "f.txt"
    target.write_bytes(b"data")
    operations = FileOperations(
        session_id=log.session_id,
        journal_path=log.path,
        get_project_root=lambda: str(tmp_path),
        flush_pending=log.writer.flush_inline,
        lock_root=tmp_path / "locks",
        event_sink=log.commit_offline,
        event_source=lambda: iter_file_operations_events(log.iter_events()),
    )
    snapshot, _ = operations.capture(str(target), encoding="utf-8")
    with operations.artifacts.write_scope(
        owner="test-replay-mutation",
        maximum_bytes=len(b"changed"),
        ttl_seconds=60,
    ) as scope:
        mutation_set = MutationSet(
            transaction_id="replay-ignored-transaction",
            session_id=log.session_id,
            source="test",
            mutations=(
                ReplaceMutation(
                    before=snapshot,
                    after=scope.put_bytes(b"changed"),
                ),
            ),
            recovery_policy=RecoveryPolicy.ROLLBACK_INCOMPLETE,
        )
        operations.mutations.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
        )

    result = replay(log)
    assert [m.content for m in result.model_context_messages] == ["hi"]
    assert result.message_events == 1


# ---------------------------------------------------------------------------
# End-to-end through the Edit tool (whole-file write + substring edit)
# ---------------------------------------------------------------------------


def _recorder(tmp_path):
    log = SessionLog("snap_sess", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent(session_id="snap_sess"))
    return None, log


def _bind_file_operations(tool, log, tmp_path, target=None):
    operations = FileOperations(
        session_id=log.session_id,
        journal_path=log.path,
        get_project_root=lambda: str(tmp_path),
        flush_pending=log.writer.flush_inline,
        lock_root=tmp_path / "locks",
        event_sink=log.commit_offline,
        event_source=lambda: iter_file_operations_events(log.iter_events()),
    )
    tool.get_cwd = lambda: str(tmp_path)

    async def plan_file_edit(request):
        return operations.plan_file_edit(request)

    async def commit_edit_plan(plan_id, **kwargs):
        return operations.commit_edit_plan(plan_id, **kwargs)

    tool.plan_file_edit = plan_file_edit
    tool.commit_edit_plan = commit_edit_plan
    if target is not None:
        snapshot, _ = operations.capture(str(target), encoding="utf-8")
        operations.observe(snapshot)
    return operations


def test_write_overwrite_captures_before_image(tmp_path):
    # Whole-file overwrite = Edit with an empty old_string (the former Write).
    target = tmp_path / "f.txt"
    target.write_text("original")

    _, log = _recorder(tmp_path)
    tool = Edit()
    _bind_file_operations(tool, log, tmp_path, target)

    asyncio.run(tool.call(file_path=str(target), old_string="", new_string="replacement"))

    assert target.read_text() == "replacement"
    entries = file_history(log)[str(target)]
    assert len(entries) == 1
    assert entries[0].pre_hash == hashlib.sha256(b"original").hexdigest()
    assert entries[0].tool == "EditPlanner"


def test_write_new_file_records_create(tmp_path):
    target = tmp_path / "new.txt"
    _, log = _recorder(tmp_path)
    tool = Edit()
    _bind_file_operations(tool, log, tmp_path)

    asyncio.run(tool.call(file_path=str(target), old_string="", new_string="fresh"))

    entries = file_history(log)[str(target)]
    assert len(entries) == 1
    assert entries[0].operation == "create"
    assert entries[0].pre_hash is None


def test_edit_captures_before_image(tmp_path):
    target = tmp_path / "f.py"
    target.write_text("a = 1\nb = 2\n")

    _, log = _recorder(tmp_path)
    tool = Edit()
    _bind_file_operations(tool, log, tmp_path, target)

    asyncio.run(tool.call(file_path=str(target), old_string="a = 1", new_string="a = 99"))

    assert "a = 99" in target.read_text()
    entries = file_history(log)[str(target)]
    assert len(entries) == 1
    assert entries[0].pre_hash == hashlib.sha256(b"a = 1\nb = 2\n").hexdigest()


def test_unbound_file_mutation_is_rejected(tmp_path):
    tool = Edit()
    try:
        asyncio.run(tool.call(file_path=str(tmp_path / "x"), new_string="x"))
    except AttributeError:
        pass
    else:
        raise AssertionError("unbound mutation unexpectedly succeeded")
