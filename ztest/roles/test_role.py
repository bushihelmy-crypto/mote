#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.roles.role.Role (construction, serialization, properties,
capabilities, messaging, and the async human/sleep helpers)."""
from __future__ import annotations

import asyncio
import os
import types

import pytest

from metagpt.common.agent_control import set_control
from metagpt.common.const import MESSAGE_ROUTE_TO_SELF
from metagpt.common.exception import RoleContextNotSetError
from metagpt.common.schema import AIMessage, Message
from metagpt.common.utils.common import any_to_str
from metagpt.roles import Role, RoleSchema, RoleState
from metagpt.roles.role_components import _dedupe_tools

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
            assert getattr(r._components, slot) is None

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
        assert role._components._router is first


class TestTurnContextBus:
    def test_slot_starts_none(self):
        assert Role(name="X")._components._turn_context_bus is None

    def test_lazy_built_and_cached(self, role):
        bus = role.turn_context_bus
        assert bus is not None
        assert role.turn_context_bus is bus  # cached
        assert role._components._turn_context_bus is bus

    def test_wires_unconditional_sources(self, role):
        # No LSP config on the default fixture => the LSP feed is absent (the
        # DiagnosticsBuffer is the source, wired only when LSP is configured).
        # Background-task progress is NOT a turn-context source: it is delivered
        # via msg_buffer notifications, so no <task-attachment> feed here.
        # The skill-activation feed is wired unconditionally (self-suppresses
        # when skills are disabled or no touched file matches a conditional one).
        bus = role.turn_context_bus
        names = {getattr(s, "name", "") for s in bus._sources}
        assert names == {"git", "token", "compaction", "skill_activation"}

    def test_lsp_source_present_when_configured(self):
        from metagpt.common.schema import LspConfig, LspServerConfig
        from metagpt.router.llm.context import Context

        cfg = LspConfig(
            enabled=True,
            servers=[LspServerConfig(name="x", command=["x"], extensions=[".py"])],
        )
        r = Role(name="X", role_schema=RoleSchema(name="X", lsp=cfg), context=Context())
        bus = r.turn_context_bus
        names = {getattr(s, "name", "") for s in bus._sources}
        assert "lsp" in names
        # The wired LSP source is the buffer itself (dual-role).
        assert r.diagnostics_buffer in bus._sources

    def test_sources_sorted_by_priority(self, role):
        bus = role.turn_context_bus
        priorities = [getattr(s, "priority", 0) for s in bus._sources]
        assert priorities == sorted(priorities)


class TestRecordTurnContext:
    """_record_turn_context persists the bus's save_to_context bucket to history."""

    class _FakeBus:
        def __init__(self, block):
            self._block = block
            self.seen_cwd = "unset"

        async def collect_to_context(self, *, cwd=None):
            self.seen_cwd = cwd
            return self._block

    def test_non_empty_block_added_to_history(self, role):
        import asyncio

        fake = self._FakeBus("<system-reminder>\ngit changed\n</system-reminder>")
        role._components._turn_context_bus = fake
        before = role.context_manager.count()
        asyncio.run(role._record_turn_context())
        msgs = role.context_manager.get()
        assert role.context_manager.count() == before + 1
        assert msgs[-1].content == "<system-reminder>\ngit changed\n</system-reminder>"
        assert msgs[-1].role == "user"

    def test_empty_block_adds_nothing(self, role):
        import asyncio

        role._components._turn_context_bus = self._FakeBus("")
        before = role.context_manager.count()
        asyncio.run(role._record_turn_context())
        assert role.context_manager.count() == before

    def test_passes_working_dir_as_cwd(self, role):
        import asyncio

        fake = self._FakeBus("")
        role._components._turn_context_bus = fake
        role.state.working_dir = "/some/dir"
        asyncio.run(role._record_turn_context())
        assert fake.seen_cwd == "/some/dir"


# =============================================================================
# Task-completion wake wiring (bg-task completion -> new turn)
# =============================================================================
class TestTaskCompletionWake:
    def test_wake_set_before_pool_built_is_applied(self, role):
        # The scheduler/REPL wires the wake at AgentRuntime construction, which
        # happens before the background pool is lazily built. The callback must
        # survive that ordering and land on the pool the builder creates.
        marker = object()
        role.set_task_completion_wake(marker)
        assert role._components._bg_pool is None  # not built yet
        pool = role._components.bg_pool  # builds the pool
        assert pool is not None
        assert pool._wake is marker

    def test_wake_set_after_pool_built_is_applied(self, role):
        pool = role._components.bg_pool  # build the pool first
        marker = object()
        role.set_task_completion_wake(marker)
        assert pool._wake is marker

    def test_pending_wake_slot_starts_none(self):
        assert Role(name="X")._components._pending_task_completion_wake is None


# =============================================================================
# Compaction-notice feed (dual-role: bus subscriber + turn_context source)
# =============================================================================
class TestCompactionNotice:
    def test_slot_starts_none(self):
        assert Role(name="X")._components._compaction_notice is None

    def test_lazy_built_and_cached(self, role):
        notice = role.compaction_notice
        assert notice is not None
        assert role.compaction_notice is notice  # cached

    def test_subscribed_to_event_bus(self, role):
        assert role.compaction_notice in role.event_bus.subscribers

    def test_same_instance_in_both_buses(self, role):
        # The object subscribed to the event bus (input edge) must be the same
        # object rendered by the turn-context bus (output edge), else the armed
        # flag set by handle() would never be seen by render().
        notice = role.compaction_notice
        assert notice in role.event_bus.subscribers
        assert notice in role.turn_context_bus._sources


# =============================================================================
# File-watch service wiring (opt-in, bus subscriber, cleanup)
# =============================================================================
class TestFileWatchService:
    def test_slot_starts_none(self):
        assert Role(name="X")._components._file_watch_service is None

    def test_none_without_config(self, role):
        # No file_watch config => watcher disabled.
        assert role.file_watch_service is None

    def test_none_when_enabled_but_no_hook_layer(self, role):
        from metagpt.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        # No hook layer (no HookConfig, no registered callback) => nothing would
        # consume FileChanged events, so the watcher stays off.
        assert role.file_watch_service is None

    def test_built_when_enabled_with_hook_layer(self, role):
        from metagpt.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        role.register_hook("FileChanged", lambda hook_input: None)
        svc = role.file_watch_service
        assert svc is not None
        assert role.file_watch_service is svc  # cached
        # The service subscribed itself to the role's event bus.
        assert svc in role.event_bus.subscribers

    def test_cleanup_stops_and_unsubscribes(self, role):
        from metagpt.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        role.register_hook("FileChanged", lambda hook_input: None)

        async def scenario():
            svc = role.file_watch_service
            svc.start()
            assert svc.watcher.is_running() is True
            await role.cleanup()
            return svc

        svc = asyncio.run(scenario())
        assert svc.watcher.is_running() is False
        assert svc not in role.event_bus.subscribers

    def test_cleanup_safe_when_watcher_never_built(self, role):
        # Nothing to stop — cleanup short-circuits without error.
        asyncio.run(role.cleanup())
        assert role._components._file_watch_service is None


class TestFileWatchHotReload:
    """The reload_skills / reload_config flags auto-wire FileChanged handlers."""

    def test_reload_skills_engages_hook_and_watches_skill_dir(self, role):
        from metagpt.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        # No manual hook registered: the auto-registered skill handler is what
        # engages the hook layer, so the service builds.
        svc = role.file_watch_service
        assert svc is not None
        skill_root = role._components.skill_manager.source_dirs()[0]
        assert os.path.abspath(skill_root) in svc.watcher._roots

    def test_skill_filechanged_fires_reload(self, role, monkeypatch):
        from metagpt.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        _ = role.file_watch_service  # builds + registers the handler
        calls = {"n": 0}

        def fake_reload():
            calls["n"] += 1
            return True

        monkeypatch.setattr(role._components.skill_manager, "reload", fake_reload)

        async def scenario():
            await role.hook_manager.fire(
                "FileChanged", {"path": "/proj/skills/demo/SKILL.md", "change_type": "modified"}
            )
            # A non-SKILL.md path must not trigger a skill reload (matcher gate).
            await role.hook_manager.fire(
                "FileChanged", {"path": "/proj/main.py", "change_type": "modified"}
            )

        asyncio.run(scenario())
        assert calls["n"] == 1

    def test_reload_config_engages_hook_and_swaps_config(self, role, monkeypatch):
        from metagpt.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_config=True)
        svc = role.file_watch_service
        assert svc is not None  # config handler alone engaged the hook layer

        sentinel = object()
        monkeypatch.setattr("metagpt.common.config.loader.load_config", lambda *a, **k: sentinel)

        async def scenario():
            await role.hook_manager.fire(
                "FileChanged", {"path": "/proj/metagpt/config.yaml", "change_type": "modified"}
            )

        asyncio.run(scenario())
        assert role.config is sentinel


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
# Fork-skill isolated execution (run_skill_fork)
# =============================================================================
class _ForkProbeRole(Role):
    """Role whose run() records the child's schema/state instead of looping."""

    captured: dict = {}

    async def run(self, with_message=None):  # type: ignore[override]
        _ForkProbeRole.captured = {
            "schema": self.role_schema,
            "state": self.state,
            "message": with_message,
        }
        self.state.last_end_output = "  child summary  "


class _ForkHandle:
    """Inline handle: runs the spawned fork child to completion, tears it down."""

    def __init__(self, role):
        self.runtime = types.SimpleNamespace(role=role)
        self._role = role

    async def run_to_completion(self, message):
        try:
            await self._role.run(with_message=message)
            return (getattr(self._role.state, "last_end_output", "") or "").strip()
        finally:
            cleanup = getattr(self._role, "cleanup", None)
            if cleanup is not None:
                await cleanup()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _InlineForkControl:
    """Minimal plane for skill-fork: builds the child via the spec factory."""

    async def spawn_agent(self, spec):
        from metagpt.common.agent_control import SpawnContext

        return _ForkHandle(spec.role_factory(SpawnContext(parent_id=spec.parent_id)))


class TestRunSkillFork:
    def _parent(self):
        parent_state = RoleState(session_id="parent-sid", working_dir="/work/dir")
        return _ForkProbeRole(role_schema=RoleSchema(name="P"), state=parent_state)

    def _run_fork(self, parent, **kwargs):
        # run_skill_fork is born on the plane through the single spawn authority;
        # bind a minimal inline plane (builds the child via the spec factory and
        # runs it to completion) so the fork has a plane to be born on.
        async def _go():
            with set_control(_InlineForkControl()):
                return await parent.run_skill_fork(**kwargs)

        return asyncio.run(_go())

    def test_returns_stripped_child_output(self):
        parent = self._parent()
        out = self._run_fork(parent, instructions="BODY", arguments="payload")
        assert out == "child summary"

    def test_child_isolated_with_lineage(self):
        parent = self._parent()
        self._run_fork(parent, instructions="BODY", arguments="x")
        child_state = _ForkProbeRole.captured["state"]
        assert child_state.parent_session_id == "parent-sid"
        assert child_state.session_id != "parent-sid"
        assert child_state.working_dir == "/work/dir"

    def test_child_schema_injects_body_and_limits_tools(self):
        parent = self._parent()
        self._run_fork(parent, instructions="SKILL BODY", allowed_tools=["Read"])
        schema = _ForkProbeRole.captured["schema"]
        assert schema.instruction == "SKILL BODY"
        assert schema.tools == ["Read"]
        # A fork skill cannot spawn its own children.
        assert schema.mcps == [] and schema.agents == [] and schema.skills == []

    def test_child_receives_arguments_as_message(self):
        parent = self._parent()
        self._run_fork(parent, instructions="B", arguments="the task")
        msg = _ForkProbeRole.captured["message"]
        assert msg.content == "the task"


# =============================================================================
# Capabilities allowlist + active signal
# =============================================================================
class TestCapabilities:
    def test_capability_map_keys(self):
        caps = Role(name="X").tool_capabilities()
        assert set(caps) == {
            "get_cwd", "set_cwd", "deactivate", "ask_human", "request_approval",
            "reply_to_human", "end_session", "record_file_read", "get_file_read_mtime",
            "record_file_snapshot", "get_tool_session", "set_tool_session",
            "record_terminal_state", "take_pending_terminal_restore",
            "record_kernel_state", "take_pending_kernel_restore",
            "record_browser_state", "take_pending_browser_restore",
            "get_browser_headless",
            "wait_interruptible", "get_bg_pool", "get_skill_pool", "run_skill_fork",
            "get_sandbox_runtime",
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
        # `slept` is wall-clock derived (time.time); only assert it's a float,
        # not non-negative, since the wall clock can skew backward (e.g. WSL2).
        assert isinstance(slept, float)

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
        r._components._context_manager = FakeContextManager()
        r._components._think_engine = FakeThinkEngine()
        r._set_active(True)
        out = asyncio.run(r.end_session())
        assert out == ""
        assert r._is_active() is False
        assert r.state.last_end_output == ""

    def test_summary_uses_summary_task_llm(self):
        r = Role(name="X")
        r.role_schema.use_summary = True
        r._components._context_manager = FakeContextManager()
        r._components._think_engine = FakeThinkEngine(_FakeThinkResult(content="found bug", is_empty=False))
        fake_llm = FakeLLM(reply="THE SUMMARY")
        r._components._router = FakeRouter(llm=fake_llm)
        out = asyncio.run(r.end_session())
        assert out == "THE SUMMARY"
        assert r.state.last_end_output == "THE SUMMARY"
        # summary peripherally routed via the dedicated SUMMARY task
        assert "summary" in r._components._router.task_calls
        assert fake_llm.aask_calls  # the summary llm was actually asked


# =============================================================================
# get_memories delegation
# =============================================================================
class TestGetMemories:
    def test_delegates_to_context_manager(self):
        r = Role(name="X")
        msgs = [Message(content="a"), Message(content="b")]
        r._components._context_manager = FakeContextManager(msgs)
        assert r.get_memories() == msgs
        assert r.get_memories(k=1) == msgs[-1:]


# =============================================================================
# tool list dedup (Bash and Terminal are distinct; dedup only)
# =============================================================================
class TestDedupeTools:
    def test_bash_kept_as_is(self):
        assert _dedupe_tools(["Bash"]) == ["Bash"]

    def test_bash_and_terminal_coexist(self):
        assert _dedupe_tools(["Bash", "Terminal"]) == ["Bash", "Terminal"]

    def test_non_shell_tools_untouched(self):
        assert _dedupe_tools(["Read", "Write"]) == ["Read", "Write"]

    def test_duplicates_removed(self):
        assert _dedupe_tools(["Terminal", "Bash", "Terminal"]) == ["Terminal", "Bash"]

    def test_order_preserved(self):
        assert _dedupe_tools(["Read", "Bash", "Write"]) == [
            "Read",
            "Bash",
            "Write",
        ]
