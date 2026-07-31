#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.session.listing`` — lite session discovery.

Covers: empty/missing base; sessions sorted newest-first by mtime; meta + first
message preview from the head; latest meta_update (title/last_prompt) from the
tail; cwd filtering on working_dir/project_root; dirs without a rollout skipped.
"""
from __future__ import annotations

import asyncio
import os

from mote.contracts.conversation import AIMessage, UserMessage
from mote.runtime.session.events import MessageEvent, MetaUpdateEvent, SessionMetaEvent
from mote.runtime.session.listing import list_sessions
from mote.runtime.session.log import SessionLog


def _append(log: SessionLog, event) -> None:
    asyncio.run(log.append(event))


def _make(tmp_path, sid, *, working_dir="/w", project_root="/p", model="gpt-4", first=None):
    log = SessionLog(sid, base_dir=str(tmp_path))
    _append(
        log,
        SessionMetaEvent(
            session_id=sid,
            working_dir=working_dir,
            project_root=project_root,
            model=model,
        ),
    )
    if first is not None:
        _append(log, MessageEvent(message=UserMessage(content=first)))
    return log


def test_empty_and_missing_base(tmp_path):
    assert list_sessions(base_dir=str(tmp_path / "nope")) == []
    (tmp_path / "empty").mkdir()
    assert list_sessions(base_dir=str(tmp_path / "empty")) == []


def test_lists_meta_and_preview(tmp_path):
    _make(tmp_path, "s1", working_dir="/w1", model="m1", first="do the thing")
    infos = list_sessions(base_dir=str(tmp_path))
    assert len(infos) == 1
    info = infos[0]
    assert info.session_id == "s1"
    assert info.working_dir == "/w1"
    assert info.model == "m1"
    assert info.preview == "do the thing"
    assert info.created_at is not None


def test_sorted_newest_first(tmp_path):
    _make(tmp_path, "old", first="old")
    log_new = _make(tmp_path, "new", first="new")
    # Force a clearly newer mtime on the second log.
    future = os.path.getmtime(log_new.path) + 100
    os.utime(log_new.path, (future, future))
    ids = [i.session_id for i in list_sessions(base_dir=str(tmp_path))]
    assert ids == ["new", "old"]


def test_tail_meta_update_title_and_last_prompt(tmp_path):
    log = _make(tmp_path, "s1", first="hello")
    _append(log, MetaUpdateEvent(title="My Session", last_prompt="fix the bug"))
    # A later update of the title should win (last-write-wins).
    _append(log, MetaUpdateEvent(title="Renamed"))
    info = list_sessions(base_dir=str(tmp_path))[0]
    assert info.title == "Renamed"
    assert info.last_prompt == "fix the bug"


def test_head_title_read_past_line_window(tmp_path):
    # TitleSubscriber appends its title fire-and-forget during the first turn,
    # so it lands after the first turn's own events — well past a tight leading
    # line window. The head byte-window must still catch it.
    log = _make(tmp_path, "s1", first="please fix the parser")
    for i in range(40):  # bury the title far past the old 16-line head window
        _append(log, MessageEvent(message=AIMessage(content=f"step {i}")))
    _append(
        log,
        MetaUpdateEvent(title="Fix The Parser", last_prompt="please fix the parser"),
    )
    info = list_sessions(base_dir=str(tmp_path))[0]
    assert info.title == "Fix The Parser"
    assert info.last_prompt == "please fix the parser"


def test_tail_rename_overrides_head_title(tmp_path):
    # A head title exists, but a later end-of-session rename in the tail wins.
    log = _make(tmp_path, "s1", first="hi")
    _append(log, MetaUpdateEvent(title="Original", last_prompt="hi"))
    _append(log, MetaUpdateEvent(title="Renamed"))
    info = list_sessions(base_dir=str(tmp_path))[0]
    assert info.title == "Renamed"
    # last_prompt has no tail override → falls back to the head value.
    assert info.last_prompt == "hi"


def test_cwd_filter(tmp_path):
    _make(tmp_path, "a", working_dir="/repo-a", project_root="/repo-a")
    _make(tmp_path, "b", working_dir="/repo-b", project_root="/repo-b")
    ids = sorted(i.session_id for i in list_sessions(base_dir=str(tmp_path), cwd="/repo-a"))
    assert ids == ["a"]


def test_cwd_filter_matches_project_root(tmp_path):
    _make(tmp_path, "a", working_dir="/sub/dir", project_root="/proj")
    ids = [i.session_id for i in list_sessions(base_dir=str(tmp_path), cwd="/proj")]
    assert ids == ["a"]


def test_dir_without_rollout_skipped(tmp_path):
    _make(tmp_path, "real", first="x")
    (tmp_path / "stray").mkdir()  # no rollout.jsonl inside
    ids = [i.session_id for i in list_sessions(base_dir=str(tmp_path))]
    assert ids == ["real"]


def test_role_classmethod_delegates(tmp_path):
    from mote.runtime.agent import Role

    _make(tmp_path, "s1", first="hi")
    infos = Role.list_sessions(str(tmp_path))
    assert [i.session_id for i in infos] == ["s1"]
