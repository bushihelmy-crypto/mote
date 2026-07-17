#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.roles.role_schema.RoleSchema (deploy-time static config)."""
from __future__ import annotations

from mote.roles.role_schema import RoleSchema


class TestDefaults:
    def test_identity_defaults(self):
        s = RoleSchema()
        assert s.name == "Zero"
        assert s.profile == "Role"

    def test_protocol_default_is_native(self):
        assert RoleSchema().command_protocol == "native"

    def test_collection_defaults(self):
        s = RoleSchema()
        assert s.mcps == []
        assert s.agents == []
        assert s.skills == []

    def test_memory_defaults(self):
        s = RoleSchema()
        assert s.enable_memory is True
        assert s.enable_router is False

    def test_behavior_flag_defaults(self):
        s = RoleSchema()
        assert s.observe_all_msg_from_buffer is True

    def test_prompt_templates_populated(self):
        s = RoleSchema()
        assert s.system_prompt
        # cmd_prompt defaults to "" by design — the trailing user prompt is now
        # assembled from per-turn context (memory + reminder sources), so the base
        # command template is intentionally empty.
        assert s.cmd_prompt == ""


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
            profile="Shipper",
            tools=["Read", "Write"],
            command_protocol="xml",
        )
        assert s.name == "Cleo"
        assert s.profile == "Shipper"
        assert s.tools == ["Read", "Write"]
        assert s.command_protocol == "xml"

    def test_round_trip_model_dump_validate(self):
        s = RoleSchema(name="Dee", tools=["Bash"])
        restored = RoleSchema.model_validate(s.model_dump())
        assert restored.name == "Dee"
        assert restored.tools == ["Bash"]
