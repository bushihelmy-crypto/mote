#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.roles.context_provider — ContextProvider + ThinkRequest.

Focus on the pure, side-effect-free assembly the provider does by READING the
Role: loop_context() packing, property forwarders, env-derived strings, and the
router-driven resolve_llm() conduit. prepare() (full prompt build + tool specs)
is exercised indirectly via its inputs since it depends on the prompt stack.
"""
from __future__ import annotations

import asyncio

from mote.common.base import LoopContext
from mote.roles import Role
from mote.roles.context_provider import ContextProvider
from mote.roles.context_provider.base import BaseContextProvider
from mote.roles.context_provider.request import ThinkRequest

from .conftest import FakeEnv


class TestThinkRequest:
    def test_dataclass_fields(self):
        tr = ThinkRequest(req=[1], system_prompt="sys", tool_specs=["t"])
        assert tr.req == [1]
        assert tr.system_prompt == "sys"
        assert tr.tool_specs == ["t"]


class TestProviderWiring:
    def test_role_returns_context_provider(self, role):
        cp = role.context_provider
        assert isinstance(cp, ContextProvider)
        assert isinstance(cp, BaseContextProvider)
        # cached
        assert role.context_provider is cp

    def test_forwarders_read_live_role(self, role):
        cp = role.context_provider
        assert cp._schema is role.role_schema
        assert cp._state is role.state


class TestLoopContext:
    def test_packs_schema_and_state(self, role):
        role.role_schema.max_react_loop = 11
        role.role_schema.max_consecutive_react_limit = 3
        role.role_schema.memory_k = 9
        role.role_schema.tools = ["Read", "Bash"]
        role.role_schema.enable_memory = False
        role.role_schema.observe_all_msg_from_buffer = False

        lc = role.context_provider.loop_context()
        assert isinstance(lc, LoopContext)
        assert lc.max_react_loop == 11
        assert lc.max_consecutive_react_limit == 3
        assert lc.memory_k == 9
        assert lc.name == role.name
        assert lc.display_name == role.role_schema.display_name
        assert lc.tools == ["Read", "Bash"]
        assert lc.enable_memory is False
        assert lc.observe_all is False
        # live RoleState references
        assert lc.msg_buffer is role.state.msg_buffer
        assert lc.watch is role.state.watch

    def test_reevaluated_per_call(self, role):
        cp = role.context_provider
        before = cp.loop_context().max_react_loop
        role.role_schema.max_react_loop = before + 5
        assert cp.loop_context().max_react_loop == before + 5


class TestEnvDerivedStrings:
    def test_env_desc_empty_without_env(self, role):
        assert role.context_provider._env_desc() == ""

    def test_env_desc_returns_env_desc(self, role):
        role.set_env(FakeEnv(desc="the world"))
        assert role.context_provider._env_desc() == "the world"

    def test_other_role_names_empty_without_env(self, role):
        assert role.context_provider._other_role_names() == ""

    def test_other_role_names_excludes_self(self, role):
        bob = Role(name="Bob")
        carol = Role(name="Carol")
        env = FakeEnv(desc="team", roles={"Bob": bob, "Carol": carol})
        role.set_env(env)
        names = role.context_provider._other_role_names()
        assert "Bob" in names and "Carol" in names
        assert role.name not in names

    def test_team_info_empty_without_env(self, role):
        assert role.context_provider._team_info() == ""

    def test_team_info_lists_roles(self, role):
        bob = Role(name="Bob", profile="Eng", goal="build")
        env = FakeEnv(desc="team", roles={"Bob": bob})
        role.set_env(env)
        info = role.context_provider._team_info()
        assert "Bob" in info
        assert "Eng" in info
        assert "build" in info


class TestResolveLLM:
    def test_fixed_model_when_router_disabled(self, role):
        role.role_schema.enable_router = False  # config flag read via role.config
        # enable_router on the config drives routing; default config has it False.
        llm = asyncio.run(role.context_provider.resolve_llm())
        # Should resolve to a concrete provider (fixed config.llm path).
        assert llm is not None

    def test_no_messages_uses_fixed_even_if_enabled(self, role, monkeypatch):
        # With routing on but no messages, the provider must use the fixed model
        # (it never invokes the async router without signals to route on).
        sentinel = object()
        monkeypatch.setattr(role.config, "enable_router", True, raising=False)
        monkeypatch.setattr(role.router, "route", lambda *, name=None, llm_config=None: sentinel)
        out = asyncio.run(role.context_provider.resolve_llm(messages=None))
        assert out is sentinel

    def test_routes_when_enabled_with_messages(self, role, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(role.config, "enable_router", True, raising=False)

        async def fake_aroute(request):
            assert request.messages  # signals were forwarded
            return sentinel

        monkeypatch.setattr(role.router, "aroute", fake_aroute)
        out = asyncio.run(role.context_provider.resolve_llm(messages=[{"role": "user", "content": "hi"}]))
        assert out is sentinel
