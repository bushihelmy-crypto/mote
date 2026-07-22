#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.roles.role_state.RoleState (serializable runtime snapshot)."""
from __future__ import annotations

from mote.common.schema import LLMCallContext, Message, MessageQueue
from mote.roles.role_state import RoleState, RoleStateController


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

    def test_session_id_is_unique_timestamped(self):
        a, b = RoleState(), RoleState()
        assert a.session_id != b.session_id
        # Format is ``{YYYYMMDDHHMMSSmmm}_{rand}`` (see session.ids.new_session_id).
        timestamp, _, rand = a.session_id.partition("_")
        assert timestamp.isdigit()
        assert len(timestamp) == 17  # YYYYMMDDHHMMSS + 3-digit millis
        assert len(rand) == 8

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

    def test_revealed_tools_default_empty_and_serialized(self):
        st = RoleState()
        assert st.revealed_tools == set()
        assert "revealed_tools" in st.model_dump()

    def test_revealed_tools_survive_resume(self):
        # Tool-search: a revealed deferred tool must stay visible after a
        # dump/reload (session resume) — the revealed set rides the checkpoint.
        st = RoleState()
        st.revealed_tools = {"ConvertImage", "QueryDatabase"}
        restored = RoleState.model_validate(st.model_dump())
        assert restored.revealed_tools == {"ConvertImage", "QueryDatabase"}


class TestRevealController:
    def test_reveal_unions_and_reads_back(self):
        ctl = RoleStateController(RoleState())
        assert ctl.get_revealed_tools() == set()
        ctl.reveal_tools(["A", "B"])
        ctl.reveal_tools(["B", "C"])  # union, idempotent on B
        assert ctl.get_revealed_tools() == {"A", "B", "C"}

    def test_get_revealed_is_live_state(self):
        st = RoleState()
        ctl = RoleStateController(st)
        ctl.reveal_tools({"X"})
        # Returns the live state set, so it reflects the checkpointed field.
        assert st.revealed_tools == {"X"}

    def test_context_messages_round_trip(self):
        st = RoleState()
        st.context.messages.append(Message(content="remembered"))
        restored = RoleState.model_validate(st.model_dump())
        assert len(restored.context.messages) == 1
        assert restored.context.messages[0].content == "remembered"


class TestTurnIndex:
    def test_default_zero_and_serialized(self):
        st = RoleState()
        assert st.turn_index == 0
        assert "turn_index" in st.model_dump()

    def test_survives_resume(self):
        # Hunk attribution: the monotonic turn counter rides the checkpoint so a
        # resumed session keeps counting from where it left off.
        st = RoleState()
        st.turn_index = 5
        restored = RoleState.model_validate(st.model_dump())
        assert restored.turn_index == 5

    def test_advance_and_read(self):
        ctl = RoleStateController(RoleState())
        assert ctl.current_turn_index() == 0
        assert ctl.advance_turn() == 1
        assert ctl.advance_turn() == 2
        assert ctl.current_turn_index() == 2


class TestRuntimeMutation:
    def test_file_read_state_is_mutable_runtime_only(self):
        st = RoleState()
        st._file_read_state["/tmp/x"] = 123
        assert st._file_read_state["/tmp/x"] == 123
        # Runtime-only: never serialized.
        assert "_file_read_state" not in st.model_dump()
