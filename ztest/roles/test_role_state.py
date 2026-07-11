#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.roles.role_state.RoleState (serializable runtime snapshot)."""
from __future__ import annotations

from mote.common.schema import LLMCallContext, Message, MessageQueue
from mote.roles.role_state import RoleState


class TestDefaults:
    def test_fresh_state_defaults(self):
        st = RoleState()
        assert isinstance(st.context, LLMCallContext)
        assert isinstance(st.msg_buffer, MessageQueue)
        assert st.latest_observed_msg is None
        assert st.recovered is False
        assert st.last_end_output == ""
        assert st.addresses == set()
        assert st.watch == set()
        assert st.env is None

    def test_session_id_is_unique_hex(self):
        a, b = RoleState(), RoleState()
        assert a.session_id != b.session_id
        assert len(a.session_id) == 32  # uuid4().hex

    def test_working_dirs_default_to_workspace(self):
        st = RoleState()
        assert st.working_dir
        assert st.original_working_dir
        assert st.project_root
        # All three start identical (the live cwd has not diverged yet).
        assert st.working_dir == st.original_working_dir == st.project_root

    def test_private_flags_default(self):
        st = RoleState()
        assert st._active is False
        assert st._memory_ready is False
        assert st._file_read_state == {}


class TestSerialization:
    def test_msg_buffer_and_env_excluded_from_dump(self):
        st = RoleState()
        dumped = st.model_dump()
        assert "msg_buffer" not in dumped
        assert "env" not in dumped
        assert "last_end_output" not in dumped  # exclude=True

    def test_serialized_fields_present(self):
        st = RoleState()
        dumped = st.model_dump()
        for key in ("context", "session_id", "working_dir", "addresses", "watch"):
            assert key in dumped

    def test_round_trip_preserves_identity_fields(self):
        st = RoleState()
        st.addresses = {"a", "b"}
        st.watch = {"WatchedAction"}
        st.recovered = True
        restored = RoleState.model_validate(st.model_dump())
        assert restored.session_id == st.session_id
        assert restored.addresses == {"a", "b"}
        assert restored.watch == {"WatchedAction"}
        assert restored.recovered is True

    def test_context_messages_round_trip(self):
        st = RoleState()
        st.context.messages.append(Message(content="remembered"))
        restored = RoleState.model_validate(st.model_dump())
        assert len(restored.context.messages) == 1
        assert restored.context.messages[0].content == "remembered"


class TestRuntimeMutation:
    def test_file_read_state_is_mutable_runtime_only(self):
        st = RoleState()
        st._file_read_state["/tmp/x"] = 123
        assert st._file_read_state["/tmp/x"] == 123
        # Runtime-only: never serialized.
        assert "_file_read_state" not in st.model_dump()
