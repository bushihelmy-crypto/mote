#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.roles.role.Role (construction, serialization, properties,
capabilities, messaging, and the async human/sleep helpers)."""
from __future__ import annotations

import asyncio

import pytest

from metagpt.common.const import MESSAGE_ROUTE_TO_SELF
from metagpt.common.exception import RoleContextNotSetError
from metagpt.common.schema import AIMessage, Message
from metagpt.common.utils.common import any_to_str
from metagpt.roles import Role, RoleSchema, RoleState
from metagpt.roles.role import _resolve_shell_tools

from .conftest import FakeContextManager, FakeEnv, FakeLLM, FakeRouter, FakeThinkEngine, _FakeThinkResult


# =============================================================================
# Construction
# =============================================================================
class TestConstruction:
    def test_name_kwarg_builds_default_schema(self):
        r = Role(name="Alice")
        assert r.name == "Alice"
        assert isinstance(r.role_schema, RoleSchema)
        assert isinstance(r.state, RoleState)

    def test_schema_kwargs_forwarded(self):
        r = Role(name="Bob", goal="ship", tools=["Read"])
        assert r.role_schema.goal == "ship"
        assert r.role_schema.tools == ["Read"]

    def test_explicit_role_schema_used(self):
        schema = RoleSchema(name="Pre", profile="Custom")
        r = Role(role_schema=schema)
        assert r.role_schema is schema
        assert r.name == "Pre"

    def test_name_overrides_schema_name(self):
        schema = RoleSchema(name="Old")
        r = Role(name="New", role_schema=schema)
        assert r.name == "New"

    def test_explicit_state_used(self):
        st = RoleState()
        r = Role(name="X", state=st)
        assert r.state is st

    def test_no_args_uses_defaults(self):
        r = Role()
        assert r.name == "Zero"

    def test_lazy_slots_start_none(self):
        r = Role(name="X")
        for slot in ("_think_engine", "_executor", "_skill_mgr", "_bg_pool",
                     "_command_channel", "_context_provider", "_context_manager", "_router"):
            assert getattr(r, slot) is None

    def test_hash_is_identity(self):
        r = Role(name="X")
        assert hash(r) == id(r)
        assert {r}  # hashable / usable in a set


# =============================================================================
# Address initialisation / recovery
# =============================================================================
class TestAddressInit:
    def test_default_addresses_include_name_and_qualified(self):
        r = Role(name="Alice")
        assert any_to_str(r) in r.state.addresses
        assert "Alice" in r.state.addresses

    def test_empty_name_addresses_only_qualified(self):
        r = Role(name="")
        assert r.state.addresses == {any_to_str(r)}

    def test_recovered_flag_set_when_latest_observed_msg(self):
        st = RoleState()
        st.latest_observed_msg = Message(content="seen")
        r = Role(name="X", state=st)
        assert r.state.recovered is True

    def test_not_recovered_on_fresh_state(self):
        assert Role(name="X").state.recovered is False

    def test_preexisting_addresses_kept(self):
        st = RoleState()
        st.addresses = {"keep-me"}
        r = Role(name="X", state=st)
        assert r.state.addresses == {"keep-me"}


# =============================================================================
# Serialization
# =============================================================================
class TestSerialization:
    def test_dump_shape(self):
        r = Role(name="Alice", goal="ship")
        data = r.dump()
        assert data["__module_class_name"] == "metagpt.roles.role.Role"
        assert data["role_schema"]["name"] == "Alice"
        assert data["role_schema"]["goal"] == "ship"
        assert "state" in data

    def test_round_trip_via_load(self):
        r = Role(name="Alice", goal="ship", tools=["Read"])
        restored = Role.load(r.dump())
        assert isinstance(restored, Role)
        assert restored.name == "Alice"
        assert restored.role_schema.goal == "ship"
        assert restored.role_schema.tools == ["Read"]

    def test_round_trip_preserves_session_id(self):
        r = Role(name="Alice")
        restored = Role.load(r.dump())
        assert restored.session_id == r.session_id

    def test_load_missing_class_name_raises(self):
        with pytest.raises(ValueError):
            Role.load({"state": {}, "role_schema": {}})

    def test_load_unknown_class_raises(self):
        with pytest.raises(TypeError):
            Role.load({"__module_class_name": "no.such.Class"})


# =============================================================================
# Properties: name / config / context
# =============================================================================
class TestProperties:
    def test_name_setter_updates_schema(self):
        r = Role(name="Alice")
        r.name = "Bob"
        assert r.name == "Bob"
        assert r.role_schema.name == "Bob"

    def test_context_raises_when_unset(self):
        with pytest.raises(RoleContextNotSetError):
            _ = Role(name="X").context

    def test_context_setter_getter(self, context):
        r = Role(name="X")
        r.context = context
        assert r.context is context

    def test_config_prefers_injected(self):
        r = Role(name="X", config="injected-config")
        assert r.config == "injected-config"

    def test_config_falls_back_to_context(self, context):
        r = Role(name="X", context=context)
        # context.config is the real Config; just assert delegation works.
        assert r.config is context.config

    def test_config_setter(self):
        r = Role(name="X")
        r.config = "cfg"
        assert r.config == "cfg"

    def test_router_lazy_cached(self, role):
        first = role.router
        assert role.router is first  # cached
        assert role._router is first


class TestTurnContextBus:
    def test_slot_starts_none(self):
        assert Role(name="X")._turn_context_bus is None

    def test_lazy_built_and_cached(self, role):
        bus = role.turn_context_bus
        assert bus is not None
        assert role.turn_context_bus is bus  # cached
        assert role._turn_context_bus is bus

    def test_wires_all_four_sources(self, role):
        bus = role.turn_context_bus
        names = {getattr(s, "name", "") for s in bus._sources}
        assert names == {"git", "token", "background_tasks", "lsp"}

    def test_sources_sorted_by_priority(self, role):
        bus = role.turn_context_bus
        priorities = [getattr(s, "priority", 0) for s in bus._sources]
        assert priorities == sorted(priorities)


# =============================================================================
# Framework properties + cwd / file-read state
# =============================================================================
class TestFrameworkProperties:
    def test_session_id_delegates(self):
        r = Role(name="X")
        assert r.session_id == r.state.session_id

    def test_env_property_and_setter(self):
        r = Role(name="X")
        assert r.env is None
        env = FakeEnv()
        r.set_env(env)
        assert r.env is env
        # set_env registers addresses with the env
        assert env.set_addresses_calls

    def test_set_env_none_does_not_register(self):
        r = Role(name="X")
        r.set_env(None)
        assert r.env is None

    def test_is_idle_true_when_buffer_empty(self):
        assert Role(name="X").is_idle is True

    def test_is_idle_false_after_message(self):
        r = Role(name="X")
        r.put_message(Message(content="hi"))
        assert r.is_idle is False

    def test_set_addresses_without_env(self):
        r = Role(name="X")
        r.set_addresses({"a", "b"})
        assert r.state.addresses == {"a", "b"}

    def test_set_addresses_with_env_propagates(self):
        r = Role(name="X")
        env = FakeEnv()
        r.set_env(env)
        env.set_addresses_calls.clear()
        r.set_addresses({"only"})
        assert env.set_addresses_calls[-1][1] == {"only"}

    def test_get_set_cwd(self):
        r = Role(name="X")
        assert r.get_cwd()  # defaults to workspace
        r.set_cwd("/tmp/work")
        assert r.get_cwd() == "/tmp/work"

    def test_get_cwd_falls_back_when_working_dir_blank(self):
        r = Role(name="X")
        r.state.working_dir = ""
        assert r.get_cwd() == r.state.original_working_dir

    def test_record_and_get_file_read(self):
        r = Role(name="X")
        assert r.get_file_read_mtime("/a") is None
        r.record_file_read("/a", 999)
        assert r.get_file_read_mtime("/a") == 999


# =============================================================================
# Capabilities allowlist + active signal
# =============================================================================
class TestCapabilities:
    def test_capability_map_keys(self):
        caps = Role(name="X").tool_capabilities()
        assert set(caps) == {
            "get_cwd", "set_cwd", "deactivate", "ask_human", "request_approval",
            "reply_to_human", "end_session", "record_file_read", "get_file_read_mtime",
            "record_file_snapshot", "wait_interruptible",
        }

    def test_capability_values_are_bound_methods(self):
        r = Role(name="X")
        caps = r.tool_capabilities()
        assert caps["get_cwd"]() == r.get_cwd()
        # Bound methods compare equal (same instance + same function).
        assert caps["set_cwd"] == r.set_cwd

    def test_active_signal_default_false(self):
        assert Role(name="X")._is_active() is False

    def test_set_active_and_deactivate(self):
        r = Role(name="X")
        r._set_active(True)
        assert r._is_active() is True
        r.deactivate()
        assert r._is_active() is False


# =============================================================================
# Messaging: put_message / publish_message
# =============================================================================
class TestMessaging:
    def test_put_message_pushes(self):
        r = Role(name="X")
        r.put_message(Message(content="hi"))
        assert not r.is_idle

    def test_put_message_ignores_falsy(self):
        r = Role(name="X")
        r.put_message(None)
        assert r.is_idle

    def test_publish_to_self_buffers_locally(self):
        r = Role(name="Alice")
        msg = Message(content="x", send_to={MESSAGE_ROUTE_TO_SELF})
        r.publish_message(msg)
        # routed to self -> buffered, sent_from filled, SELF replaced
        assert not r.is_idle
        assert MESSAGE_ROUTE_TO_SELF not in msg.send_to
        assert msg.sent_from == any_to_str(r)

    def test_publish_to_own_name_buffers_locally(self):
        r = Role(name="Alice")
        msg = Message(content="x", send_to={"Alice"})
        r.publish_message(msg)
        assert not r.is_idle

    def test_publish_to_other_without_env_drops(self):
        r = Role(name="Alice")
        msg = Message(content="x", send_to={"Bob"})
        r.publish_message(msg)
        # no env, not addressed to self -> silently dropped (stays idle)
        assert r.is_idle

    def test_publish_to_other_with_env_routes_to_env(self):
        r = Role(name="Alice")
        env = FakeEnv()
        r.set_env(env)
        msg = Message(content="x", send_to={"Bob"})
        r.publish_message(msg)
        assert env.published == [msg]

    def test_publish_falsy_noop(self):
        r = Role(name="Alice")
        r.publish_message(None)
        assert r.is_idle

    def test_publish_aimessage_gets_agent_tag(self):
        r = Role(name="Alice", profile="Eng")
        env = FakeEnv()
        r.set_env(env)
        msg = AIMessage(content="x", send_to={"Bob"})
        r.publish_message(msg)
        assert env.published[0].agent == r.role_schema.display_name


# =============================================================================
# Async helpers: ask_human / reply_to_human
# =============================================================================
class TestHumanChannel:
    def test_ask_human_requires_question(self):
        r = Role(name="X")
        assert asyncio.run(r.ask_human("")).startswith("Error:")

    def test_ask_human_without_env(self):
        r = Role(name="X")
        assert "Not in MGXEnv" in asyncio.run(r.ask_human("hello?"))

    def test_ask_human_returns_env_response(self):
        r = Role(name="Alice")
        env = FakeEnv()
        env.human_response = "blue"
        r.set_env(env)
        assert asyncio.run(r.ask_human("favourite colour?")) == "blue"
        assert env.human_questions[0] == ("favourite colour?", "Alice")

    def test_ask_human_stop_deactivates(self):
        r = Role(name="Alice")
        r._set_active(True)
        env = FakeEnv()
        env.human_response = "please stop"
        r.set_env(env)
        out = asyncio.run(r.ask_human("continue?"))
        assert "encountered a problem" in out
        assert r._is_active() is False

    def test_reply_to_human_requires_content(self):
        r = Role(name="X")
        assert asyncio.run(r.reply_to_human("")).startswith("Error:")

    def test_reply_to_human_without_env(self):
        r = Role(name="X")
        assert "Not in MGXEnv" in asyncio.run(r.reply_to_human("hi"))

    def test_reply_to_human_delegates(self):
        r = Role(name="Alice")
        env = FakeEnv()
        r.set_env(env)
        assert asyncio.run(r.reply_to_human("done")) == "delivered"
        assert env.human_replies[0] == ("done", "Alice")


# =============================================================================
# wait_interruptible
# =============================================================================
class TestWaitInterruptible:
    def test_completes_without_activity(self):
        r = Role(name="X")
        slept, interrupted = asyncio.run(r.wait_interruptible(0.1))
        assert interrupted is False
        assert slept >= 0.0

    def test_interrupted_by_message(self):
        r = Role(name="X")

        async def scenario():
            task = asyncio.create_task(r.wait_interruptible(5.0))
            await asyncio.sleep(0.05)
            r.put_message(Message(content="wake"))
            return await asyncio.wait_for(task, timeout=2.0)

        slept, interrupted = asyncio.run(scenario())
        assert interrupted is True
        assert slept < 5.0


# =============================================================================
# end_session
# =============================================================================
class TestEndSession:
    def test_no_summary_returns_empty(self):
        r = Role(name="X")
        r.role_schema.use_summary = False
        r._context_manager = FakeContextManager()
        r._think_engine = FakeThinkEngine()
        r._set_active(True)
        out = asyncio.run(r.end_session())
        assert out == ""
        assert r._is_active() is False
        assert r.state.last_end_output == ""

    def test_summary_uses_summary_task_llm(self):
        r = Role(name="X")
        r.role_schema.use_summary = True
        r._context_manager = FakeContextManager()
        r._think_engine = FakeThinkEngine(_FakeThinkResult(content="found bug", is_empty=False))
        fake_llm = FakeLLM(reply="THE SUMMARY")
        r._router = FakeRouter(llm=fake_llm)
        out = asyncio.run(r.end_session())
        assert out == "THE SUMMARY"
        assert r.state.last_end_output == "THE SUMMARY"
        # summary peripherally routed via the dedicated SUMMARY task
        assert "summary" in r._router.task_calls
        assert fake_llm.aask_calls  # the summary llm was actually asked


# =============================================================================
# get_memories delegation
# =============================================================================
class TestGetMemories:
    def test_delegates_to_context_manager(self):
        r = Role(name="X")
        msgs = [Message(content="a"), Message(content="b")]
        r._context_manager = FakeContextManager(msgs)
        assert r.get_memories() == msgs
        assert r.get_memories(k=1) == msgs[-1:]


# =============================================================================
# shell_tool resolution (Bash always -> terminal)
# =============================================================================
class TestResolveShellTools:
    def test_terminal_replaces_bash(self):
        assert _resolve_shell_tools(["Bash"]) == ["terminal"]

    def test_non_bash_tools_untouched(self):
        assert _resolve_shell_tools(["Read", "Write"]) == ["Read", "Write"]

    def test_explicit_terminal_tool_preserved_and_deduped(self):
        # Bash + explicitly listed terminal -> no duplicate terminal.
        assert _resolve_shell_tools(["terminal", "Bash"]) == ["terminal"]

    def test_order_preserved(self):
        assert _resolve_shell_tools(["Read", "Bash", "Write"]) == [
            "Read",
            "terminal",
            "Write",
        ]
