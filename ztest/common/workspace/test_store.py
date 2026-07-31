#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.runtime.session.workspace.SessionWorkspace`.

The store is the single owner of the on-disk layout: every path a writer needs
is derived here, co-located under one session directory.
"""
from __future__ import annotations

from mote.runtime.session.workspace import SessionSpace, SessionWorkspace


class TestSessionWorkspaceLayout:
    def test_paths_co_locate_under_session_dir(self, tmp_path):
        store = SessionWorkspace(tmp_path)
        assert store.root == tmp_path
        assert store.sessions_root == tmp_path / ".agent_sessions"
        sess = store.session_dir("abc")
        assert sess == tmp_path / ".agent_sessions" / "abc"
        # rollout + every artifact space live under the one session directory.
        assert store.rollout_path("abc") == sess / "rollout.jsonl"
        assert store.space("abc", SessionSpace.TOOL_RESULTS) == sess / "tool_results"
        assert store.space("abc", SessionSpace.TASK_OUTPUTS) == sess / "task_outputs"

    def test_empty_session_falls_back_to_default_bucket(self, tmp_path):
        store = SessionWorkspace(tmp_path)
        assert store.session_dir("").name == "default"

    def test_iter_session_ids(self, tmp_path):
        store = SessionWorkspace(tmp_path)
        # No sessions root yet -> empty, side-effect-free.
        assert list(store.iter_session_ids()) == []
        for sid in ("s1", "s2"):
            store.session_dir(sid).mkdir(parents=True)
        # A stray file under the sessions root is ignored (dirs only).
        (store.sessions_root / "stray.txt").write_text("x")
        assert sorted(store.iter_session_ids()) == ["s1", "s2"]

    def test_defaults_to_workspace_root(self):
        # No explicit root -> the standard workspace root (ends in /workspace).
        store = SessionWorkspace()
        assert store.root.name == "workspace"
