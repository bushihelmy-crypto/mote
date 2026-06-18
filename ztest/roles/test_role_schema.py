#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.roles.role_schema.RoleSchema (deploy-time static config)."""
from __future__ import annotations

from metagpt.roles.role_schema import RoleSchema


class TestDefaults:
    def test_identity_defaults(self):
        s = RoleSchema()
        assert s.name == "Zero"
        assert s.profile == "Role"
        assert s.goal == ""
        assert s.constraints == ""
        assert s.role_id == ""

    def test_protocol_default_is_native(self):
        assert RoleSchema().command_protocol == "native"

    def test_loop_control_defaults(self):
        s = RoleSchema()
        assert s.max_react_loop == 50
        assert s.max_consecutive_react_limit == 10

    def test_collection_defaults(self):
        s = RoleSchema()
        assert s.tools == [
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "Bash",
            "Terminal",
            "Jupyter",
            "Agent",
            "AskUserQuestion",
            "Sleep",
            "ResumeTasks",
            "CancelTasks",
            "GetNodeState",
            "MediaPipeline",
        ]
        assert s.mcps == []
        assert s.agents == []
        assert s.skills == []

    def test_memory_summary_defaults(self):
        s = RoleSchema()
        assert s.enable_memory is True
        assert s.memory_k == 30
        assert s.use_summary is True
        assert s.enable_router is False

    def test_behavior_flag_defaults(self):
        s = RoleSchema()
        assert s.delegated_from == ""
        assert s.observe_all_msg_from_buffer is True
        # ClassVar — not a model field, shared default
        assert s.need_end_recommendations_tag is False

    def test_prompt_templates_populated(self):
        s = RoleSchema()
        assert s.system_prompt
        assert s.cmd_prompt
        assert s.instruction
        assert s.summary_prompt
        assert s.summary_with_recommend_prompt


class TestDisplayName:
    def test_with_profile(self):
        assert RoleSchema(name="Bob", profile="Engineer").display_name == "Bob(Engineer)"

    def test_without_profile_falls_back_to_name(self):
        assert RoleSchema(name="Bob", profile="").display_name == "Bob"

    def test_default(self):
        assert RoleSchema().display_name == "Zero(Role)"


class TestOverrides:
    def test_kwargs_override_defaults(self):
        s = RoleSchema(
            name="Cleo",
            goal="ship it",
            tools=["Read", "Write"],
            max_react_loop=7,
            command_protocol="xml",
        )
        assert s.name == "Cleo"
        assert s.goal == "ship it"
        assert s.tools == ["Read", "Write"]
        assert s.max_react_loop == 7
        assert s.command_protocol == "xml"

    def test_need_recommendations_is_classvar_not_field(self):
        # Being a ClassVar, it must not appear as a serialized model field.
        assert "need_end_recommendations_tag" not in RoleSchema.model_fields

    def test_round_trip_model_dump_validate(self):
        s = RoleSchema(name="Dee", tools=["Bash"], memory_k=5)
        restored = RoleSchema.model_validate(s.model_dump())
        assert restored.name == "Dee"
        assert restored.tools == ["Bash"]
        assert restored.memory_k == 5
