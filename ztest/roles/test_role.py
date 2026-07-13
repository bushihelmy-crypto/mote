#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.roles.role.Role (construction, serialization, properties,
capabilities, messaging, and the async human/sleep helpers)."""
from __future__ import annotations

import asyncio
import os
import types

import pytest

from mote.common.agent_control import set_control
from mote.common.const import MESSAGE_ROUTE_TO_SELF
from mote.common.exception import RoleContextNotSetError
from mote.common.schema import AIMessage, Message
from mote.common.utils.common import any_to_str
from mote.roles import Role, RoleSchema, RoleState
from mote.roles.role_components import _dedupe_tools

from .conftest import FakeContextManager, FakeEnv


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
        r = Role(name="Bob", profile="Shipper", tools=["Read"])
        assert r.role_schema.profile == "Shipper"
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
        graph = r._components._graph
        for name in (
            "think_engine_factory",
            "think_subsystems_factory",
            "loop_factory",
            "executor",
            "skill_manager",
            "bg_pool",
            "command_channel",
            "context_provider",
            "context_manager",
            "router",
            "state_ctl",
            "capabilities",
            "session_manager",
        ):
            assert graph.is_built(name) is False

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
        r = Role(name="Alice", profile="Shipper")
        data = r.dump()
        assert data["__module_class_name"] == "mote.roles.role.Role"
        assert data["role_schema"]["name"] == "Alice"
        assert data["role_schema"]["profile"] == "Shipper"
        assert "state" in data

    def test_round_trip_via_load(self):
        r = Role(name="Alice", profile="Shipper", tools=["Read"])
        restored = Role.load(r.dump())
        assert isinstance(restored, Role)
        assert restored.name == "Alice"
        assert restored.role_schema.profile == "Shipper"
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
        assert role._components._graph.peek("router") is first


class TestTurnContextBus:
    def test_slot_starts_none(self):
        assert Role(name="X")._components._graph.is_built("turn_context_bus") is False

    def test_lazy_built_and_cached(self, role):
        bus = role.turn_context_bus
        assert bus is not None
        assert role.turn_context_bus is bus  # cached
        assert role._components._graph.peek("turn_context_bus") is bus

    def test_wires_unconditional_sources(self, role):
        # No LSP config on the default fixture => the LSP feed is absent (the
        # DiagnosticsBuffer is the source, wired only when LSP is configured).
        # Background-task progress is NOT a turn-context source: it is delivered
        # via msg_buffer notifications, so no <task-attachment> feed here.
        # The skill-activation and skill-listing feeds are wired unconditionally
        # (self-suppress when skills are disabled or nothing matches).
        # The changed-files feed is wired unconditionally too (self-suppresses
        # when no tracked file changed on disk since it was last read).
        # GitContextSource is wired unconditionally (self-suppresses off-repo;
        # renders the nearest repo containing cwd).
        # TeamContextSource is wired unconditionally (self-suppresses when no
        # control plane is bound / the agent has no parent/siblings/children).
        # ToolCatalogContextSource is wired unconditionally (self-suppresses under
        # native tool-use, where tools ride the API tools= param instead).
        # CodeMapContextSource is wired unconditionally (self-suppresses with no
        # touched files or nothing structural to say about them).
        # There is NO per-turn cwd feed: working_dir is a stable base equal to the
        # startup dir the system prompt's env block already cites, so a reminder
        # would just repeat cacheable content. The timestamp feed (per-turn
        # wall-clock) is wired unconditionally in the structured reminder envelope.
        # The fold-pressure feed is wired unconditionally alongside token-pressure
        # (self-suppresses until the reconstructable-result count nears the fold
        # trigger).
        bus = role.turn_context_bus
        names = {getattr(s, "name", "") for s in bus._sources}
        assert names == {
            "tool_catalog",
            "git",
            "team",
            "timestamp",
            "token",
            "fold",
            "compaction",
            "skill_activation",
            "skill_listing",
            "changed_files",
            "code_map",
        }

    def test_lsp_source_present_when_configured(self):
        from mote.common.schema import LspConfig, LspServerConfig
        from mote.router.llm.context import Context

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

    def test_dual_role_sources_auto_subscribed_from_roster(self, role):
        # The single roster drives both edges: every feed exposing ``handle``
        # (a dual-role ObservationSubscriber) is auto-subscribed to the event
        # bus; render-only feeds are not. This is the invariant that lets adding
        # a feed touch exactly one list.
        from mote.common.interface import ObservationSubscriber

        roster = role._components.turn_context_sources
        subscribers = _wired_subscribers(role)
        for src in roster:
            if isinstance(src, ObservationSubscriber):
                assert src in subscribers, f"{src.name} (dual-role) not subscribed"
            else:
                assert src not in subscribers, f"{src.name} (render-only) subscribed"

    def test_single_roster_shared_by_both_buses(self, role):
        # The event-bus wiring and the turn-context bus must read the SAME
        # roster instances (not two parallel builds), or a dual-role feed's
        # armed state would be invisible to its renderer.
        roster = role._components.turn_context_sources
        assert role.turn_context_bus._sources and set(roster) == set(role.turn_context_bus._sources)

    def test_last_render_reports_injection_manifest(self, role):
        import asyncio

        bus = role.turn_context_bus
        asyncio.run(bus.collect_to_context(cwd=None))
        # Every persisted source is accounted for in the manifest (True/False).
        persisted = {s.name for s in bus._sources if getattr(s, "save_to_context", True)}
        assert persisted <= set(bus.last_render)


def _wired_subscribers(role) -> list:
    """Wire the spine (as the run lifecycle does) then return the bus subscribers.

    The ``event_bus`` getter is a pure leaf — an unwired spine by design — so a
    test that inspects the roster must first perform the same explicit wiring step
    the runtime performs in ``Role._ensure_ready``.
    """
    role._components._wire_spine()
    return role.event_bus.subscribers


class TestEventSubscriberRoster:
    """The single declarative roster wires all subscribers (infra + dual-role)."""

    def test_infra_observers_always_present(self, role):
        # Recorder + logger are unconditional observers; both must be on the bus
        # regardless of any opt-in layer.
        from mote.common.events.log_subscriber import LogSubscriber
        from mote.session.subscribers import RecorderSubscriber

        subs = _wired_subscribers(role)
        assert any(isinstance(s, RecorderSubscriber) for s in subs)
        assert any(isinstance(s, LogSubscriber) for s in subs)

    def test_optin_subscribers_absent_on_bare_role(self, role):
        # No hook layer, no LSP, no tracing/reporter env => none of the opt-in
        # subscribers are wired (the roster drops the ``None`` entries).
        from mote.common.hook.subscriber import HookSubscriber
        from mote.roles.lsp.service import LspService

        subs = _wired_subscribers(role)
        assert not any(isinstance(s, HookSubscriber) for s in subs)
        assert not any(isinstance(s, LspService) for s in subs)

    def test_hook_subscriber_wired_when_hook_layer_exists(self, role):
        from mote.common.hook.subscriber import HookSubscriber

        role.register_hook("Stop", lambda hook_input: None)
        subs = _wired_subscribers(role)
        assert any(isinstance(s, HookSubscriber) for s in subs)

    def test_secret_subscribers_absent_when_secrets_disabled(self, role):
        # secrets disabled → neither secret subscriber wired and the shared store
        # getter is None (no vault touched). Force-disable rather than rely on the
        # default so the test is hermetic even when the dev's ~/.mote/config.yaml
        # turns the layer on.
        from mote.executor.secrets.subscriber import RedactionSubscriber, SecretUploadSubscriber

        role.config.secrets.enabled = False
        subs = _wired_subscribers(role)
        assert not any(isinstance(s, (SecretUploadSubscriber, RedactionSubscriber)) for s in subs)
        assert role._components.secret_store is None

    def test_secret_subscribers_wired_when_secrets_enabled(self, role, tmp_path, monkeypatch):
        # Enabling the layer wires BOTH secret subscribers onto the shared store.
        # Redirect the key file + vault into tmp and skip config discovery so the
        # test never touches the real ~/.mote or harvests a real config.yaml.
        from mote.executor.secrets.subscriber import RedactionSubscriber, SecretUploadSubscriber
        from mote.roles import role_components as rc

        monkeypatch.setattr("mote.common.secrets.cipher.CONFIG_ROOT", tmp_path)
        monkeypatch.setattr(rc, "_primary_config_path", lambda cwd: None)
        role.config.secrets.enabled = True
        role.config.secrets.vault_path = str(tmp_path / "vault.json")

        store = role._components.secret_store
        assert store is not None
        subs = _wired_subscribers(role)
        assert any(isinstance(s, SecretUploadSubscriber) for s in subs)
        assert any(isinstance(s, RedactionSubscriber) for s in subs)

    def test_lsp_service_wired_and_gets_bus_backref(self):
        # The LSP service is an observer that also *produces* DiagnosticsEvent:
        # subscribing it must hand it the bus via ``on_subscribed`` (no host
        # special-case), and it must land on the bus.
        from mote.common.schema import LspConfig, LspServerConfig
        from mote.roles.lsp.service import LspService
        from mote.router.llm.context import Context

        cfg = LspConfig(
            enabled=True,
            servers=[LspServerConfig(name="x", command=["x"], extensions=[".py"])],
        )
        r = Role(name="X", role_schema=RoleSchema(name="X", lsp=cfg), context=Context())
        subs = _wired_subscribers(r)
        lsp = next((s for s in subs if isinstance(s, LspService)), None)
        assert lsp is not None
        assert lsp.bus is r.event_bus

    def test_event_bus_getter_is_a_pure_leaf(self, role):
        # Reading ``event_bus`` constructs a bare spine with zero subscribers —
        # wiring is a *separate* lifecycle step, never a getter side-effect. This
        # is the property that keeps construction a pure DAG (no component read can
        # transitively pull a wired bus, so no construction cycle can form).
        bus = role.event_bus
        assert bus.subscribers == []
        assert role._components._spine_wired is False

    def test_construction_order_independent(self):
        # Reading ``context_manager`` first (it reads ``event_bus`` back during its
        # own construction) must not double-build or deadlock: the build graph is a
        # DAG (the leaf bus carries no subscribers), so access order is immaterial.
        from mote.router.llm.context import Context

        r = Role(name="Y", role_schema=RoleSchema(name="Y"), context=Context())
        cm = r.context_manager  # trigger via the manager edge first
        assert r.event_bus is r._components._graph.peek("event_bus")  # same leaf, reused
        assert r.context_manager is cm  # same instance — not rebuilt

    def test_wire_spine_is_idempotent(self, role):
        # Wiring twice must not double-subscribe (the spine wires exactly once).
        role._components._wire_spine()
        before = list(role.event_bus.subscribers)
        role._components._wire_spine()  # redundant call (e.g. a second _ensure_ready)
        assert role.event_bus.subscribers == before

    def test_context_manager_getter_does_not_wire_the_reducer(self, role):
        # Reading ``context_manager`` is a pure build: it must NOT mutate the
        # sibling router as a hidden side-effect. The reducer edge is a separate
        # lifecycle step (``_wire_collaborators``), so the router stays clean
        # until it runs.
        _ = role.context_manager
        assert role.router.context_reducer is None
        assert role._components._collaborators_wired is False

    def test_wire_collaborators_stamps_reducer(self, role):
        # The explicit wiring step establishes the router ← ContextManager reducer
        # edge (COMPRESS recovery) — the one runtime cross-reference between built
        # collaborators.
        role._components._wire_collaborators()
        assert role.router.context_reducer is role.context_manager.recovery_reducer
        assert role._components._collaborators_wired is True

    def test_wire_collaborators_is_idempotent(self, role):
        role._components._wire_collaborators()
        first = role.router.context_reducer
        role._components._wire_collaborators()  # redundant call
        assert role.router.context_reducer is first

    def test_summarize_llm_is_reducer_less(self, role):
        # The safety invariant behind the no-runtime-guard design: the LLM the
        # ContextManager's summarize reducer issues its inner aask() on MUST carry
        # no COMPRESS reducer, or that inner overflow would recurse
        # _compress → summarize → forever. It is the router's COMPRESSION-variant
        # instance (route_for_task(COMPRESSION_TASK)); stamping the router's reducer
        # afterwards must NOT leak onto it.
        cm = role.context_manager
        role._components._wire_collaborators()  # stamps router.context_reducer
        summarize_llm = cm._summarize._llm
        assert summarize_llm is cm._llm  # same instance summarize runs on
        assert summarize_llm.context_reducer is None
        # And it is genuinely distinct from a reducer-bearing think instance.
        assert role.router.context_reducer is not None


# =============================================================================
# Skills explicit per-role specification wiring
# =============================================================================
class TestSkillsWiring:
    """The Skills subsystem engages purely on the config switch
    (``config.context.skills.enabled``): listing skills does NOT implicitly turn
    it on. When enabled, a role's ``skills`` list narrows to an include filter."""

    @staticmethod
    def _role(context, *, global_on, **schema_kwargs):
        r = Role(name="X", role_schema=RoleSchema(name="X", **schema_kwargs), context=context)
        # Pin the ambient project config so the assertions isolate the switch.
        r.config.context.skills.enabled = global_on
        return r

    def test_listing_skills_does_not_enable_with_switch_off(self, context):
        r = self._role(context, global_on=False, skills=["foo"])
        assert r._components.skill_manager._enabled is False

    def test_switch_on_enables_and_narrows_to_listed(self, context):
        r = self._role(context, global_on=True, skills=["foo"])
        mgr = r._components.skill_manager
        assert mgr._enabled is True  # config switch engages the subsystem
        assert mgr._skills == ["foo"]  # narrows to an include filter

    def test_switch_off_keeps_subsystem_disabled(self, context):
        r = self._role(context, global_on=False, skills=[])
        assert r._components.skill_manager._enabled is False

    def test_executor_exposes_skill_tool_when_switch_on(self, context):
        # tools deliberately omits "Skill"; the enabled subsystem still exposes
        # the bridge tool.
        r = self._role(context, global_on=True, tools=["Read"], skills=["foo"])
        assert "Skill" in r._components.executor._tools

    def test_executor_omits_skill_tool_when_switch_off(self, context):
        r = self._role(context, global_on=False, tools=["Read"], skills=["foo"])
        assert "Skill" not in r._components.executor._tools


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
        assert role._components._graph.is_built("bg_pool") is False  # not built yet
        pool = role._components.bg_pool  # builds the pool
        assert pool is not None
        assert pool._wake is marker

    def test_wake_set_after_pool_built_is_applied(self, role):
        pool = role._components.bg_pool  # build the pool first
        marker = object()
        role.set_task_completion_wake(marker)
        assert pool._wake is marker

    def test_pending_wake_slot_starts_none(self):
        assert Role(name="X")._components._state.pending_task_completion_wake is None


# =============================================================================
# Compaction-notice feed (dual-role: bus subscriber + turn_context source)
# =============================================================================
class TestCompactionNotice:
    def test_roster_slot_starts_none(self):
        # No feed has a private slot anymore; the whole roster is built once.
        assert Role(name="X")._components._graph.is_built("turn_context_sources") is False

    def test_lookup_by_name_and_cached(self, role):
        notice = role.turn_context_source("compaction")
        assert notice is not None
        assert role.turn_context_source("compaction") is notice  # cached roster

    def test_subscribed_to_event_bus(self, role):
        assert role.turn_context_source("compaction") in _wired_subscribers(role)

    def test_same_instance_in_both_buses(self, role):
        # The object subscribed to the event bus (input edge) must be the same
        # object rendered by the turn-context bus (output edge), else the armed
        # flag set by handle() would never be seen by render().
        notice = role.turn_context_source("compaction")
        assert notice in _wired_subscribers(role)
        assert notice in role.turn_context_bus._sources


# =============================================================================
# File-watch service wiring (opt-in, bus subscriber, cleanup)
# =============================================================================
class TestFileWatchService:
    def test_slot_starts_none(self):
        assert Role(name="X")._components._graph.is_built("file_watch_service") is False

    def test_none_without_config(self, role):
        # No file_watch config => watcher disabled.
        assert role.file_watch_service is None

    def test_none_when_enabled_but_no_hook_layer(self, role):
        from mote.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        # No hook layer (no HookConfig, no registered callback) => nothing would
        # consume FileChanged events, so the watcher stays off.
        assert role.file_watch_service is None

    def test_built_when_enabled_with_hook_layer(self, role):
        from mote.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        role.register_hook("FileChanged", lambda hook_input: None)
        svc = role.file_watch_service
        assert svc is not None
        assert role.file_watch_service is svc  # cached
        # The service subscribed itself to the role's event bus.
        assert svc in role.event_bus.subscribers

    def test_cleanup_stops_and_unsubscribes(self, role):
        from mote.common.schema import FileWatchConfig

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
        assert role._components._graph.is_built("file_watch_service") is False


class TestFileWatchHotReload:
    """The reload_skills / reload_config flags auto-wire FileChanged handlers."""

    def test_reload_skills_engages_hook_and_watches_skill_dir(self, role):
        from mote.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        # No manual hook registered: the auto-registered skill handler is what
        # engages the hook layer, so the service builds.
        svc = role.file_watch_service
        assert svc is not None
        skill_root = role._components.skill_manager.source_dirs()[0]
        assert os.path.abspath(skill_root) in svc.watcher._roots

    def test_skill_filechanged_fires_reload(self, role, monkeypatch):
        from mote.common.schema import FileWatchConfig

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
            await role.hook_manager.fire("FileChanged", {"path": "/proj/main.py", "change_type": "modified"})

        asyncio.run(scenario())
        assert calls["n"] == 1

    def test_base_root_watched_only_inside_a_git_repo(self, role, monkeypatch):
        """The whole-tree base root is the git root — added only when cwd is in a repo.

        The base root is the one entry that pulls the *entire* tree into the
        watcher's baseline walk. Gating it on a real git root keeps a launch from
        a home / large directory (no repo) from recursively walking the whole
        tree — the reload_* config dirs are watched regardless.
        """
        from mote.common.schema import FileWatchConfig

        monkeypatch.setattr("mote.roles.role_components.find_git_root", lambda cwd: "/proj/repo")
        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        svc = role.file_watch_service
        assert os.path.abspath("/proj/repo") in svc.watcher._roots

    def test_base_root_omitted_outside_a_git_repo(self, role, monkeypatch):
        """Outside a repo the cwd is NOT added — no whole-tree walk from home."""
        from mote.common.schema import FileWatchConfig

        monkeypatch.setattr("mote.roles.role_components.find_git_root", lambda cwd: None)
        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        svc = role.file_watch_service
        # The cwd is never pulled in as a root when there is no repo boundary.
        cwd = os.path.abspath(role.get_cwd())
        assert cwd not in svc.watcher._roots
        # Hot-reload still works: the skill dir is present.
        skill_root = role._components.skill_manager.source_dirs()[0]
        assert os.path.abspath(skill_root) in svc.watcher._roots

    def test_reload_config_engages_hook_and_swaps_config(self, role, monkeypatch):
        from mote.common.schema import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_config=True)
        svc = role.file_watch_service
        assert svc is not None  # config handler alone engaged the hook layer

        sentinel = object()
        monkeypatch.setattr("mote.roles.role_components.load_config", lambda *a, **k: sentinel)

        async def scenario():
            await role.hook_manager.fire("FileChanged", {"path": "/proj/mote/config.yaml", "change_type": "modified"})

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
        from mote.common.agent_control import SpawnContext

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
        assert "SKILL BODY" in schema.system_prompt
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
            "get_cwd",
            "set_cwd",
            "deactivate",
            "ask_user",
            "ask_user_question",
            "request_approval",
            "reply_to_user",
            "end_session",
            "record_file_read",
            "get_file_read_mtime",
            "record_file_glimpsed",
            "is_resource_visible",
            "record_file_snapshot",
            "get_tool_session",
            "set_tool_session",
            "record_terminal_state",
            "take_pending_terminal_restore",
            "record_kernel_state",
            "take_pending_kernel_restore",
            "record_browser_state",
            "take_pending_browser_restore",
            "get_browser_headless",
            "get_browser_stealth",
            "get_browser_locale",
            "get_browser_proxy",
            "wait_interruptible",
            "get_bg_pool",
            "get_skill_pool",
            "run_skill_fork",
            "register_resource",
            "get_sandbox_runtime",
            "dispatch_tool",
            "list_tool_names",
            "list_graph_tool_names",
        }

    def test_capability_values_are_bound_methods(self):
        r = Role(name="X")
        caps = r.tool_capabilities()
        assert caps["get_cwd"]() == r.get_cwd()
        # Bound methods compare equal (same instance + same function).
        assert caps["set_cwd"] == r.set_cwd


# =============================================================================
# Browser proxy resolution (role_schema override else global config.proxy)
# =============================================================================
class TestBrowserProxy:
    @staticmethod
    def _clear_proxy_env(monkeypatch):
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(var, raising=False)

    def test_falls_back_to_global_config_proxy(self, monkeypatch):
        # No per-role browser_proxy -> read the global config.yaml `proxy`.
        self._clear_proxy_env(monkeypatch)
        r = Role(name="X")
        r.config = types.SimpleNamespace(tools=types.SimpleNamespace(proxy="http://gw:8080"))
        assert r.get_browser_proxy() == "http://gw:8080"

    def test_role_schema_overrides_global(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        r = Role(name="X", role_schema=RoleSchema(name="X", browser_proxy="socks5://a:1"))
        r.config = types.SimpleNamespace(tools=types.SimpleNamespace(proxy="http://gw:8080"))
        assert r.get_browser_proxy() == "socks5://a:1"

    def test_falls_back_to_env_proxy(self, monkeypatch):
        # Neither schema nor config set -> pick up the ambient HTTP(S)_PROXY.
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setenv("HTTPS_PROXY", "http://192.168.88.121:7890")
        r = Role(name="X")
        r.config = types.SimpleNamespace(tools=types.SimpleNamespace(proxy=""))
        assert r.get_browser_proxy() == "http://192.168.88.121:7890"

    def test_empty_when_nothing_set(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        r = Role(name="X")
        r.config = types.SimpleNamespace(tools=types.SimpleNamespace(proxy=""))
        assert r.get_browser_proxy() == ""


# =============================================================================
# Browser locale resolution (role_schema override else global config.browser_locale)
# =============================================================================
class TestBrowserLocale:
    def test_falls_back_to_global_config_locale(self):
        # role_schema "auto" -> read the global config.yaml `browser_locale`.
        r = Role(name="X")
        r.config = types.SimpleNamespace(tools=types.SimpleNamespace(browser_locale="zh"))
        assert r.get_browser_locale() == "zh"

    def test_role_schema_overrides_global(self):
        r = Role(name="X", role_schema=RoleSchema(name="X", browser_locale="en"))
        r.config = types.SimpleNamespace(tools=types.SimpleNamespace(browser_locale="zh"))
        assert r.get_browser_locale() == "en"

    def test_auto_when_neither_set(self):
        r = Role(name="X")
        r.config = types.SimpleNamespace(tools=types.SimpleNamespace(browser_locale="auto"))
        assert r.get_browser_locale() == "auto"

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
# Async helpers: ask_user / reply_to_user
# =============================================================================
class TestHumanChannel:
    def test_ask_user_requires_question(self):
        r = Role(name="X")
        assert asyncio.run(r.ask_user("")).startswith("Error:")

    def test_ask_user_without_env(self):
        r = Role(name="X")
        assert "Not in MoteEnv" in asyncio.run(r.ask_user("hello?"))

    def test_ask_user_returns_env_response(self):
        r = Role(name="Alice")
        env = FakeEnv()
        env.human_response = "blue"
        r.set_env(env)
        assert asyncio.run(r.ask_user("favourite colour?")) == "blue"
        assert env.human_questions[0] == ("favourite colour?", "Alice")

    def test_ask_user_stop_deactivates(self):
        r = Role(name="Alice")
        r._set_active(True)
        env = FakeEnv()
        env.human_response = "please stop"
        r.set_env(env)
        out = asyncio.run(r.ask_user("continue?"))
        assert "encountered a problem" in out
        assert r._is_active() is False

    def test_reply_to_user_requires_content(self):
        r = Role(name="X")
        assert asyncio.run(r.reply_to_user("")).startswith("Error:")

    def test_reply_to_user_without_env(self):
        r = Role(name="X")
        assert "Not in MoteEnv" in asyncio.run(r.reply_to_user("hi"))

    def test_reply_to_user_delegates(self):
        r = Role(name="Alice")
        env = FakeEnv()
        r.set_env(env)
        assert asyncio.run(r.reply_to_user("done")) == "delivered"
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
    def test_deactivates_and_returns_empty(self):
        # end_session no longer generates a summary — it just deactivates so the
        # run loop terminates. The terminal reply is captured into
        # last_end_output by the run loop's post-loop finalization, not here.
        r = Role(name="X")
        r._set_active(True)
        out = asyncio.run(r.end_session())
        assert out == ""
        assert r._is_active() is False


# =============================================================================
# get_memories delegation
# =============================================================================
class TestGetMemories:
    def test_delegates_to_context_manager(self):
        r = Role(name="X")
        msgs = [Message(content="a"), Message(content="b")]
        r._components._graph.seed("context_manager", FakeContextManager(msgs))
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


# =============================================================================
# Auto-continue seam (_should_auto_continue) — framework only, default off
# =============================================================================
class TestAutoContinue:
    """The TurnEnd auto-continue decision: a control subscriber blocking the stop
    (``TurnOutcome.block=True``) forces another turn, bounded by
    max_auto_continue."""

    def _outcome(self, **kw):
        from mote.common.events import TurnOutcome

        return TurnOutcome(**kw)

    def test_default_budget_zero_never_continues(self):
        r = Role(name="A")
        out = self._outcome(block=True, additional_context=["go on"])
        # budget 0 → no continuation, no message enqueued (byte-identical to old).
        assert r._should_auto_continue(out, 0) is False
        assert r.state.msg_buffer.empty()

    def test_block_with_budget_continues_and_enqueues_context(self):
        r = Role(name="A")
        out = self._outcome(block=True, additional_context=["keep working"])
        assert r._should_auto_continue(out, 2) is True
        assert not r.state.msg_buffer.empty()
        msg = r.state.msg_buffer.pop()
        assert "keep working" in msg.content

    def test_block_falls_back_to_system_message(self):
        r = Role(name="A")
        out = self._outcome(block=True, system_message="not done yet")
        assert r._should_auto_continue(out, 1) is True
        msg = r.state.msg_buffer.pop()
        assert "not done yet" in msg.content

    def test_none_outcome_does_not_continue(self):
        r = Role(name="A")
        assert r._should_auto_continue(None, 3) is False
        assert r.state.msg_buffer.empty()

    def test_non_blocking_outcome_does_not_continue(self):
        r = Role(name="A")
        out = self._outcome(block=False)
        assert r._should_auto_continue(out, 3) is False
        assert r.state.msg_buffer.empty()

    def test_block_without_context_continues_without_enqueue(self):
        r = Role(name="A")
        out = self._outcome(block=True)
        assert r._should_auto_continue(out, 1) is True
        assert r.state.msg_buffer.empty()  # nothing to inject, but still continues

    def test_schema_default_is_zero(self):
        assert RoleSchema().max_auto_continue == 0


# =============================================================================
# Full-resolution smoke test — the CI backstop for the ComponentGraph
# =============================================================================
class TestFullResolutionSmoke:
    """Force every opt-in layer on, then resolve the *whole* graph.

    The engine only makes construction *cycles* structurally detectable; two soft
    gaps remain by design: (a) a ``ctx.dep("typo")`` is validated lazily — it only
    raises ``UnknownComponentError`` when that line actually resolves, so a typo
    buried in an opt-in branch (hook / lsp / sandbox / file-watch) lurks until the
    layer is enabled at deploy time; (b) a cycle that only forms across two opt-in
    branches never fires on a bare Role. This test closes both by building a Role
    with every ``available`` predicate flipped true and then resolving *every*
    registered spec — so a dep-name typo (``UnknownComponentError``) or a
    cross-branch construction cycle (``ComponentCycleError``) fails in CI instead
    of only at a customer's fully-configured deploy.
    """

    @staticmethod
    def _fully_configured_role(context):
        """A Role whose schema flips every opt-in ``available`` gate true."""
        from mote.common.schema import (
            FileWatchConfig,
            HookConfig,
            LspConfig,
            LspServerConfig,
            PermissionConfig,
            SandboxRuntimeConfig,
        )

        schema = RoleSchema(
            name="FullyLoaded",
            # hook layer: a declared HookConfig makes _hook_available true.
            hooks=HookConfig(),
            # LSP layer: enabled + >=1 server makes _lsp_available true (the
            # server is never spawned here — construction is lazy).
            lsp=LspConfig(
                enabled=True,
                servers=[LspServerConfig(name="fake", command=["true"], extensions=[".py"])],
            ),
            # OS-level sandbox: permissions.runtime enabled makes
            # _sandbox_available true (build_runtime is side-effect-free).
            permissions=PermissionConfig(runtime=SandboxRuntimeConfig(enabled=True)),
            # file-watch: enabled + the hook layer above (its FileChanged
            # consumer) makes _build_file_watch_service return a real service.
            file_watch=FileWatchConfig(enabled=True),
        )
        return Role(role_schema=schema, context=context)

    def test_every_spec_resolves_without_error(self, context):
        # The core assertion: resolving each registered component exercises its
        # whole builder body, so a dep-name typo or a cross-branch cycle surfaces
        # here (as UnknownComponentError / ComponentCycleError) rather than at a
        # fully-configured deploy.
        role = self._fully_configured_role(context)
        graph = role._components._graph
        for name in list(graph._specs):
            graph.get(name)  # must not raise

    def test_all_optin_layers_actually_built(self, context):
        # Guards against the test silently passing because a gate stayed off (a
        # None slot would skip the builder body we mean to exercise). With every
        # layer forced on, each opt-in component must resolve to a real object.
        role = self._fully_configured_role(context)
        graph = role._components._graph
        for name in list(graph._specs):
            graph.get(name)
        for name in ("hook_manager", "lsp_service", "diagnostics_buffer", "sandbox_runtime", "file_watch_service"):
            assert graph.peek(name) is not None, f"opt-in layer {name!r} was not built"
