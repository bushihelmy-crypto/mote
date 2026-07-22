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
from mote.common.output_binding import negotiate_output_binding
from mote.common.schema import OutputRepresentationCapabilities
from mote.roles import Role
from mote.roles.context_provider import ContextProvider
from mote.roles.context_provider.base import BaseContextProvider
from mote.roles.context_provider.request import ThinkRequest

from .conftest import FakeEnv


class TestThinkRequest:
    def test_dataclass_fields(self):
        binding = negotiate_output_binding(
            is_text=True,
            capabilities=OutputRepresentationCapabilities(supports_text=True),
        )
        tr = ThinkRequest(
            req=[1],
            system_prompt="sys",
            tool_specs=["t"],
            output_binding=binding,
            command_channel="channel",
            output_schema={},
            schema_fingerprint="fingerprint",
        )
        assert tr.req == [1]
        assert tr.system_prompt == "sys"
        assert tr.tool_specs == ["t"]
        assert tr.output_binding is binding
        assert tr.command_channel == "channel"


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
        role.role_schema.tools = ["Read", "Bash"]
        role.role_schema.enable_memory = False
        role.role_schema.observe_all_msg_from_buffer = False

        lc = role.context_provider.loop_context()
        assert isinstance(lc, LoopContext)
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
        role.role_schema.tools = ["Read"]
        assert cp.loop_context().tools == ["Read"]
        role.role_schema.tools = ["Read", "Write"]
        assert cp.loop_context().tools == ["Read", "Write"]


class TestResolveLLM:
    def test_fixed_model_when_router_disabled(self, role):
        # router.routing_enabled is the single routing gate; the default config
        # (strategy None) leaves it False, so the fixed models.default is used.
        assert role.router.routing_enabled is False
        llm = asyncio.run(role.context_provider.resolve_llm())
        # Should resolve to a concrete provider (fixed config.llm path).
        assert llm is not None

    def test_no_messages_uses_fixed_even_if_enabled(self, role, monkeypatch):
        # With routing on but no messages, the provider must use the fixed model
        # (it never invokes the async router without signals to route on).
        sentinel = object()
        role.router.routing_enabled = True
        monkeypatch.setattr(role.router, "route", lambda *, name=None, llm_config=None: sentinel)
        out = asyncio.run(role.context_provider.resolve_llm(messages=None))
        assert out is sentinel

    def test_routes_when_enabled_with_messages(self, role, monkeypatch):
        sentinel = object()
        # A routing role has ``routing_enabled`` True (set by _build_router for
        # any concrete strategy); the default config leaves it False.
        role.router.routing_enabled = True
        captured = {}

        async def fake_aroute(request):
            assert request.messages  # signals were forwarded
            captured["session_key"] = request.session_key
            return sentinel

        monkeypatch.setattr(role.router, "aroute", fake_aroute)
        out = asyncio.run(role.context_provider.resolve_llm(messages=[{"role": "user", "content": "hi"}]))
        assert out is sentinel
        # L2: routing state must be keyed to this role's session, not "default".
        assert captured["session_key"] == role.session_id
        assert captured["session_key"] != "default"

    def test_finalize_reprofiles_binding_and_tool_specs_for_routed_llm(self, role, monkeypatch):
        from types import SimpleNamespace

        routed = object()
        binding = negotiate_output_binding(
            is_text=True,
            capabilities=OutputRepresentationCapabilities(
                supports_text=True,
                protocol="native",
                provider="routed-provider",
                model="routed-model",
            ),
        )

        class RoutedChannel:
            def output_binding_decision(self, *, is_text):
                assert is_text is True
                return binding

            def tool_specs(self, executor, output_contract):
                assert executor is role.executor
                assert output_contract is role.output_contract
                return ["routed-spec"]

        monkeypatch.setattr(
            role.command_channel,
            "for_llm",
            lambda llm, **_kwargs: RoutedChannel() if llm is routed else None,
        )
        request = SimpleNamespace(
            output_binding=None,
            tool_specs=None,
            command_channel=None,
            output_schema={},
        )

        result = role.context_provider.finalize_for_llm(request, routed)

        assert result is request
        assert result.output_binding is binding
        assert result.tool_specs == ["routed-spec"]
        assert isinstance(result.command_channel, RoutedChannel)

    def test_fixed_model_when_routing_disabled_on_router(self, role, monkeypatch):
        # With messages present but the router's strategy None (routing_enabled
        # False), the provider must NOT route — it runs the fixed models.default.
        role.router.routing_enabled = False

        async def fail_aroute(request):
            raise AssertionError("must not route when routing_enabled is False")

        monkeypatch.setattr(role.router, "aroute", fail_aroute)
        sentinel = object()
        monkeypatch.setattr(role.router, "route", lambda *, name=None, llm_config=None: sentinel)
        out = asyncio.run(role.context_provider.resolve_llm(messages=[{"role": "user", "content": "hi"}]))
        assert out is sentinel
