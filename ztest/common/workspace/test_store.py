#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.common.workspace.WorkspaceStore`.

The store is the single owner of the on-disk layout: every path a writer needs
is derived here, co-located under one session directory.
"""
from __future__ import annotations

from mote.common.workspace import ArtifactKind, WorkspaceStore


class TestWorkspaceStoreLayout:
    def test_paths_co_locate_under_session_dir(self, tmp_path):
        store = WorkspaceStore(tmp_path)
        assert store.root == tmp_path
        assert store.sessions_root == tmp_path / ".agent_sessions"
        sess = store.session_dir("abc")
        assert sess == tmp_path / ".agent_sessions" / "abc"
        # rollout + every artifact space live under the one session directory.
        assert store.rollout_path("abc") == sess / "rollout.jsonl"
        assert store.space("abc", ArtifactKind.TOOL_RESULTS) == sess / "tool_results"
        assert store.space("abc", ArtifactKind.TASK_OUTPUTS) == sess / "task_outputs"
        assert store.space("abc", ArtifactKind.BLOBS) == sess / "blobs"

    def test_empty_session_falls_back_to_default_bucket(self, tmp_path):
        store = WorkspaceStore(tmp_path)
        assert store.session_dir("").name == "default"

    def test_iter_session_ids(self, tmp_path):
        store = WorkspaceStore(tmp_path)
        # No sessions root yet -> empty, side-effect-free.
        assert list(store.iter_session_ids()) == []
        for sid in ("s1", "s2"):
            store.session_dir(sid).mkdir(parents=True)
        # A stray file under the sessions root is ignored (dirs only).
        (store.sessions_root / "stray.txt").write_text("x")
        assert sorted(store.iter_session_ids()) == ["s1", "s2"]

    def test_legacy_dirs(self, tmp_path):
        store = WorkspaceStore(tmp_path)
        assert store.legacy_dirs() == [tmp_path / ".tool_results", tmp_path / ".task_outputs"]

    def test_defaults_to_workspace_root(self):
        # No explicit root -> the standard workspace root (ends in /workspace).
        store = WorkspaceStore()
        assert store.root.name == "workspace"
