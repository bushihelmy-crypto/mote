#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.runtime.agent.role.Role (construction, serialization, properties,
capabilities, messaging, and the async human/sleep helpers)."""
from __future__ import annotations

import asyncio
import os
import types

import pytest

from mote.contracts.constants.messages import MESSAGE_ROUTE_TO_SELF
from mote.contracts.policy.run_completion import RunCompletionDecision
from mote.contracts.schema import AIMessage, Message
from mote.contracts.services import ResolvedServiceResponse, ServiceExecutionSemantics, ServiceResponse
from mote.kernel.output import text_output_contract
from mote.product.agents import CodingAgentFactory
from mote.runtime.agent import AgentDependencies, AgentWiring, Role, RoleSchema, RoleState
from mote.runtime.agent.control import set_control
from mote.runtime.agent.execution import any_to_str
from mote.runtime.agent.runtime_modules import dedupe_tools
from mote.runtime.errors import RoleContextNotSetError
from mote.runtime.services import EngineServices
from mote.runtime.tools.execution_context import bind_tool_call_id

from .conftest import FakeContextManager, FakeEnv, FakeLLM

coding_agent_factory = CodingAgentFactory()


def _context_with_gateway(llm):
    from mote.runtime.models.clients.context import Context
    from mote.ztest.model_fakes import FakeModelGateway, offline_config

    context = Context(config=offline_config(), provider_factory=lambda config: llm)
    context.model_gateway = FakeModelGateway(llm)
    return context


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
            "flow_engine_factory",
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
        assert data["type_id"] == "mote.agent.role.v1"
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

    def test_load_missing_type_id_raises(self):
        with pytest.raises(ValueError):
            Role.load({"state": {}, "role_schema": {}})

    def test_load_unknown_class_raises(self):
        with pytest.raises(TypeError):
            Role.load({"type_id": "no.such.type.v1"})

    def test_load_legacy_module_path(self):
        r = Role(name="Legacy")
        data = r.dump()
        data.pop("type_id")
        data["__module_class_name"] = "mote.roles.role.Role"
        restored = Role.load(data)
        assert isinstance(restored, Role)
        assert restored.name == "Legacy"


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

    def test_bind_services_sets_context(self, context):
        r = Role(name="X")
        r.bind_services(EngineServices(context=context))
        assert r.context is context

    def test_config_prefers_injected(self):
        r = Role(name="X", config="injected-config")
        assert r.config == "injected-config"

    def test_config_falls_back_to_context(self, context):
        r = Role(name="X", wiring=AgentWiring.for_context(context))
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
        # The default model does not advertise server-side native tool search, so
        # deferred tools are absent from the native wire until reveal; only their
        # compact one-line hints ride the reminder tail.
        bus = role.turn_context_bus
        names = {getattr(s, "name", "") for s in bus._sources}
        # The credential index is OPT-IN and gated on the ambient user config
        # (context.turn_context.credential_index) + secrets + WebBrowser, so its
        # presence is developer-config-dependent — discount it here so this
        # default-roster assertion stays deterministic across machines.
        names.discard("credential_index")
        assert names == {
            "tool_catalog",
            "toolset_instructions",
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
            "split_tool_menu",
        }

    def test_lsp_source_present_when_configured(self):
        from mote.contracts.settings.lsp import LspConfig, LspServerConfig

        cfg = LspConfig(
            enabled=True,
            servers=[LspServerConfig(name="x", command=["x"], extensions=[".py"])],
        )
        r = Role(
            name="X",
            role_schema=RoleSchema(name="X", lsp=cfg),
            wiring=AgentWiring.for_context(_context_with_gateway(FakeLLM())),
        )
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
        # is registered with Telemetry; render-only feeds are not. This lets adding
        # a feed touch exactly one list.
        roster = role._components.turn_context_sources
        subscribers = _wired_subscribers(role)
        for src in roster:
            if callable(getattr(src, "handle", None)):
                assert src in subscribers, f"{src.name} (dual-role) not subscribed"
            else:
                assert src not in subscribers, f"{src.name} (render-only) subscribed"

    def test_single_roster_shared_by_telemetry_and_turn_context(self, role):
        # Telemetry and the TurnContextBus must read the SAME
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
    """Return the declarative structural-handler roster."""
    return role._components._build_telemetry_subscribers()


class TestTelemetryHandlerRoster:
    """The declarative roster wires infrastructure and dual-role handlers."""

    def test_infra_observers_always_present(self, role):
        # Logging is unconditional; durable recording is owned by EventFabric.
        from mote.runtime.events.log_subscriber import LogSubscriber

        subs = _wired_subscribers(role)
        assert any(isinstance(s, LogSubscriber) for s in subs)

    def test_correctness_projections_are_not_telemetry_subscribers(self, role):
        subs = _wired_subscribers(role)
        assert role.context_manager not in subs
        rebuild_sources = [
            source
            for source in role.turn_context_bus._sources
            if callable(getattr(source, "on_model_context_rebuilt", None))
        ]
        assert rebuild_sources
        assert not any(source in subs for source in rebuild_sources)

    def test_optin_subscribers_absent_on_bare_role(self, role):
        # No hook layer, no LSP, no tracing/reporter env => none of the opt-in
        # subscribers are wired (the roster drops the ``None`` entries).
        from mote.runtime.agent.lsp.service import LspService
        from mote.runtime.hook.subscriber import HookSubscriber

        subs = _wired_subscribers(role)
        assert not any(isinstance(s, HookSubscriber) for s in subs)
        assert not any(isinstance(s, LspService) for s in subs)

    def test_hook_subscriber_wired_when_hook_layer_exists(self, role):
        from mote.runtime.hook.subscriber import HookSubscriber

        role.register_hook("Stop", lambda hook_input: None)
        subs = _wired_subscribers(role)
        assert any(isinstance(s, HookSubscriber) for s in subs)

    def test_secret_store_is_always_wired_but_io_lazy(self, role, tmp_path, monkeypatch):
        from mote.runtime.agent.runtime_modules import integrations

        monkeypatch.setattr(integrations, "_primary_config_path", lambda cwd: None)
        role.config.secrets.vault_path = str(tmp_path / "vault.json")
        store = role._components.secret_store
        assert role.prompt_policy._secret_store is store
        assert not (tmp_path / "vault.json").exists()

    def test_secret_store_wired_into_prompt_and_result_policy_when_enabled(self, role, tmp_path, monkeypatch):
        # Enabling the layer injects one vault into both domain policies.
        # Redirect the key file + vault into tmp and skip config discovery so the
        # test never touches the real ~/.mote or harvests a real config.yaml.
        from mote.runtime.agent.runtime_modules import integrations

        monkeypatch.setattr("mote.runtime.secrets.cipher.CONFIG_ROOT", tmp_path)
        monkeypatch.setattr(integrations, "_primary_config_path", lambda cwd: None)
        role.config.secrets.vault_path = str(tmp_path / "vault.json")

        store = role._components.secret_store
        assert store is not None
        assert not (tmp_path / "vault.key").exists()
        assert not (tmp_path / "vault.json").exists()
        assert role.prompt_policy._secret_store is store
        assert role.tool_result_policy is not None

    def test_checkpoint_subscriber_absent_when_disabled(self, role):
        # ``record_checkpoints=False`` drops the /rewind recorder regardless of
        # backend — the builder returns None so it never lands on the bus.
        from mote.runtime.session.subscribers import CheckpointSubscriber

        role.role_schema.record_checkpoints = False
        subs = _wired_subscribers(role)
        assert not any(isinstance(s, CheckpointSubscriber) for s in subs)

    def test_checkpoint_subscriber_absent_on_non_git_backend(self, role, monkeypatch):
        # A non-repo workspace (blob backend) leaves the feature inert — no
        # per-turn capture is wired even with the flag on.
        from mote.runtime.agent.runtime_modules import session as session_module
        from mote.runtime.session.subscribers import CheckpointSubscriber

        role.role_schema.record_checkpoints = True
        monkeypatch.setattr(session_module, "checkpoint_supported", lambda _wd: False)
        subs = _wired_subscribers(role)
        assert not any(isinstance(s, CheckpointSubscriber) for s in subs)

    def test_checkpoint_subscriber_wired_on_git_backend(self, role, monkeypatch):
        # A git-backed code workspace engages the whole-tree checkpoint recorder.
        from mote.runtime.agent.runtime_modules import session as session_module
        from mote.runtime.session.subscribers import CheckpointSubscriber

        role.role_schema.record_checkpoints = True
        monkeypatch.setattr(session_module, "checkpoint_supported", lambda _wd: True)
        subs = _wired_subscribers(role)
        assert any(isinstance(s, CheckpointSubscriber) for s in subs)

    def test_lsp_service_wired_and_gets_telemetry_backref(self):
        # The LSP service both consumes FileMutatedEvent and produces
        # DiagnosticsEvent on the same Role Telemetry runtime.
        from mote.contracts.settings.lsp import LspConfig, LspServerConfig
        from mote.runtime.agent.lsp.service import LspService

        cfg = LspConfig(
            enabled=True,
            servers=[LspServerConfig(name="x", command=["x"], extensions=[".py"])],
        )
        r = Role(
            name="X",
            role_schema=RoleSchema(name="X", lsp=cfg),
            wiring=AgentWiring.for_context(_context_with_gateway(FakeLLM())),
        )

        async def scenario():
            await r._components._wire_telemetry()
            handlers = [worker.binding.handler for worker in r.telemetry._workers.values()]
            lsp = next((s for s in handlers if isinstance(s, LspService)), None)
            assert lsp is not None
            assert lsp._telemetry is r.telemetry
            await r.telemetry.aclose()

        asyncio.run(scenario())

    def test_telemetry_getter_is_a_pure_leaf(self, role):
        # Reading telemetry constructs a bare runtime with zero handlers —
        # wiring is a *separate* lifecycle step, never a getter side-effect. This
        # is the property that keeps construction a pure DAG (no component read can
        # transitively wire Telemetry, so no construction cycle can form).
        telemetry = role.telemetry
        assert telemetry.snapshots() == ()
        assert role._components._telemetry_wired is False

    def test_construction_order_independent(self):
        # Reading ``context_manager`` first (it reads telemetry during its
        # own construction) must not double-build or deadlock: the build graph is a
        # DAG, so access order is immaterial.
        r = Role(
            name="Y",
            role_schema=RoleSchema(name="Y"),
            wiring=AgentWiring.for_context(_context_with_gateway(FakeLLM())),
        )
        cm = r.context_manager  # trigger via the manager edge first
        assert r.telemetry is r._components._graph.peek("telemetry")
        assert r.context_manager is cm  # same instance — not rebuilt

    def test_wire_telemetry_is_idempotent(self, role):
        async def scenario():
            await role._components._wire_telemetry()
            before = role.telemetry.snapshots()
            await role._components._wire_telemetry()
            assert role.telemetry.snapshots() == before
            await role.telemetry.aclose()

        asyncio.run(scenario())

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
        # Compression uses a dedicated canonical route without the request
        # transformer that may invoke ContextManager recovery. Therefore its
        # inner model call cannot recurse through compression again.
        cm = role.context_manager
        role._components._wire_collaborators()  # stamps router.context_reducer
        summarize_route = cm._summarize._model_route
        assert summarize_route is cm._model_route
        assert summarize_route.request_transformer is None
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
        r = coding_agent_factory.build(
            Role,
            name="X",
            role_schema=RoleSchema(name="X", **schema_kwargs),
            services=EngineServices(context=context),
        )
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
# Tool-search deferral wiring (deferred_tools -> SearchTools + index source)
# =============================================================================
class TestToolSearchWiring:
    """A non-empty ``deferred_tools`` auto-binds the ``SearchTools`` meta-tool and
    the byte-stable index turn-context source; an empty list wires neither (zero
    overhead when unused). Deferral filters schema visibility on both channels
    and rejects guessed calls until revelation, which lives on RoleState."""

    @staticmethod
    def _role(context, *, tools, deferred):
        schema = RoleSchema(name="X", tools=tools, deferred_tools=deferred)
        return coding_agent_factory.build(Role, name="X", role_schema=schema, services=EngineServices(context=context))

    def test_deferred_tools_autobinds_search_tool(self, context):
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        assert "SearchTools" in r._components.executor._tools

    def test_no_deferral_binds_neither(self, context):
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=[])
        assert "SearchTools" not in r._components.executor._tools
        # No index source is added to the turn-context roster.
        assert r.turn_context_source("deferred_tool_index") is None

    def test_split_menu_present_without_native_tool_search(self, context):
        # Native transport alone is insufficient: the selected model must also
        # advertise server-side tool search. The default test model does not, so
        # it gets one-line menu hints and no deferred schema before reveal.
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        assert r.turn_context_source("deferred_tool_index") is None
        assert r.turn_context_source("split_tool_menu") is not None

    def test_deferred_tool_present_with_defer_loading_on_anthropic_native(self, context):
        # Anthropic native: the deferred tool's schema STAYS on the native wire
        # (the API needs every definition to expand tool_reference blocks) but
        # carries ``defer_loading:true``. Revealing it does NOT change the wire —
        # defer_loading is keyed on corpus membership, so the tools= prefix is
        # byte-stable (prompt cache preserved). No XML projection exists on this executor.
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        # Native (anthropic) wire: present + deferred, even before any reveal.
        # The server-side path is now capability-gated, so a capable model name
        # must be threaded (the executor gets it from the resolved model config).
        before = r.executor.native_tool_specs("anthropic", model="claude-opus-4-8")
        wb = next(s for s in before if s["name"] == "WebBrowser")
        assert wb.get("defer_loading") is True
        # Reveal it — the native wire is byte-identical (cache-stable).
        r.reveal_tools(["WebBrowser"])
        after = r.executor.native_tool_specs("anthropic", model="claude-opus-4-8")
        assert after == before

    def test_index_source_present_on_xml_fallback(self, context):
        # XML has no server-side defer_loading, so the client-side menu (and
        # withhold/reveal) stays — proving the gating is anthropic-native only.
        schema = RoleSchema(
            name="X",
            tools=["Read", "WebBrowser"],
            deferred_tools=["WebBrowser"],
            command_protocol="xml",
        )
        r = coding_agent_factory.build(Role, name="X", role_schema=schema, services=EngineServices(context=context))
        assert r.turn_context_source("deferred_tool_index") is not None

    @staticmethod
    def _openai_role(context, *, model, deferred=("WebBrowser",)):
        # A native role forced onto a genuine OpenAI endpoint; the model name
        # decides whether the immutable Gateway profile advertises native tool
        # search. Configure the route before constructing the role and snapshot.
        from mote.contracts.config.llm import LLMType
        from mote.ztest.model_fakes import FakeModelGateway

        schema = RoleSchema(
            name="X",
            tools=["Read", "WebBrowser"],
            deferred_tools=list(deferred),
            command_protocol="native",
        )
        d = context.config.models.default
        d.api_type = LLMType.OPENAI
        d.base_url = "https://api.openai.com/v1"
        d.model = model
        llm = FakeLLM()
        llm.model = model
        context.model_gateway = FakeModelGateway(llm)
        r = coding_agent_factory.build(Role, name="X", role_schema=schema, services=EngineServices(context=context))
        return r

    def test_index_present_on_capable_openai_native(self, context):
        # gpt-5.4+ on the genuine OpenAI endpoint takes the Responses server-side
        # tool_search path. Same as Anthropic native: the full defs are withheld
        # from context, but discovery goes through mote's own SearchTools, so the
        # compact DeferredToolIndex menu is still wired (the model's browsable
        # view of the deferred corpus). SPLIT is not used on this path.
        r = self._openai_role(context, model="gpt-5.4")
        assert r.turn_context_source("deferred_tool_index") is not None
        assert r.turn_context_source("split_tool_menu") is None

    def test_split_menu_present_on_incapable_openai_native(self, context):
        # An older OpenAI model has no native tool search → falls back to the
        # client-side path (native): the split_tool_menu source is wired (one-line
        # hints on the reminder tail), NOT the deferred_tool_index source.
        r = self._openai_role(context, model="gpt-4o")
        assert r.turn_context_source("split_tool_menu") is not None
        assert r.turn_context_source("deferred_tool_index") is None

    def test_deferred_tool_withheld_until_reveal_on_incapable_openai_native(self, context):
        r = self._openai_role(context, model="gpt-4o")
        before = r.executor.native_tool_specs("openai", model="gpt-4o")
        assert not any(spec["function"]["name"] == "WebBrowser" for spec in before)
        r.reveal_tools(["WebBrowser"])
        after = r.executor.native_tool_specs("openai", model="gpt-4o")
        wb = next(spec for spec in after if spec["function"]["name"] == "WebBrowser")
        assert wb["function"]["description"] != ""
        assert "defer_loading" not in wb

    @pytest.mark.asyncio
    async def test_guessed_deferred_tool_is_rejected_until_reveal(self, context):
        r = self._openai_role(context, model="gpt-4o")

        result = await r.executor.run_command("WebBrowser", {})

        assert result.success is False
        assert "SearchTools" in result.output
        assert r.state.revealed_tools == set()

    def test_index_lists_deferred_tool(self, context):
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        index = r.list_deferred_tools()
        assert "WebBrowser" in index
        assert index["WebBrowser"]  # a non-empty one-line description
        assert "Read" not in index  # non-deferred never in the menu

    def test_reveal_accepts_only_deferred_names(self, context):
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        # A non-deferred / unknown name is rejected (intersection with the menu).
        assert r.reveal_tools(["Read", "Bogus"]) == []
        assert r.state.revealed_tools == set()
        # A real deferred name is accepted and recorded on RoleState.
        assert r.reveal_tools(["WebBrowser"]) == ["WebBrowser"]
        assert r.state.revealed_tools == {"WebBrowser"}

    # -- global master switch (config.tools.tool_search.enabled) --------------
    def test_master_switch_default_true_engages(self, context):
        # Default config → the switch is on, so a non-empty deferred_tools engages
        # the machinery exactly as before (SearchTools bound).
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        assert r.config.tools.tool_search.enabled is True
        assert "SearchTools" in r._components.executor._tools

    def test_master_switch_off_unbinds_search_tool(self, context):
        # enabled=False forces the effective deferred set EMPTY → SearchTools is
        # NOT bound even though deferred_tools is declared (plain no-deferral).
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        r.config.tools.tool_search.enabled = False
        assert "SearchTools" not in r._components.executor._tools

    def test_master_switch_off_shows_all_tools_on_native_wire(self, context):
        # With the switch off, the corpus tool is fully visible on this Native channel.
        r = self._role(context, tools=["Read", "WebBrowser"], deferred=["WebBrowser"])
        r.config.tools.tool_search.enabled = False
        specs = r.executor.native_tool_specs("anthropic", model="claude-opus-4-8")
        wb = next(s for s in specs if s["name"] == "WebBrowser")
        assert "defer_loading" not in wb  # not deferred → no stamp

    def test_master_switch_off_suppresses_menu_on_xml(self, context):
        # Even on the XML client-side path (which normally shows the menu), the
        # switch-off empty set means no index source is wired.
        schema = RoleSchema(
            name="X",
            tools=["Read", "WebBrowser"],
            deferred_tools=["WebBrowser"],
            command_protocol="xml",
        )
        r = coding_agent_factory.build(Role, name="X", role_schema=schema, services=EngineServices(context=context))
        r.config.tools.tool_search.enabled = False
        assert r.turn_context_source("deferred_tool_index") is None

    # -- DeviceUse registration (default schema) ------------------------------
    def test_deviceuse_registered_in_default_schema(self):
        # DeviceUse ships in the default toolset AND is deferred by default
        # (small, heavy, niche — search-gated like WebBrowser).
        schema = RoleSchema(name="X")
        assert "DeviceUse" in schema.tools
        assert "DeviceUse" in schema.deferred_tools

    def test_deviceuse_deferred_but_searchable(self, context):
        # A deferred tool stays bound internally but is not dispatchable until
        # reveal; it appears in the searchable menu. Pin the master
        # switch on BEFORE building (the deferred set is fixed at build time) —
        # a sibling test mutates the shared config cache off.
        context.config.tools.tool_search.enabled = True
        schema = RoleSchema(name="X", tools=["Read", "DeviceUse"], deferred_tools=["DeviceUse"])
        r = coding_agent_factory.build(Role, name="X", role_schema=schema, services=EngineServices(context=context))
        assert "DeviceUse" in r._components.executor._tools  # bound
        assert "DeviceUse" in r.list_deferred_tools()  # searchable in the menu


# =============================================================================
# Turn-context registry: the global opt-out blacklist + the credential index
# =============================================================================
class TestTurnContextRegistry:
    """`context.turn_context.disabled` is a name blacklist that suppresses any
    normally-on ephemeral source; the credential index is the one OPT-IN source,
    CONSTRUCTED only when (toggle on) AND (WebBrowser equipped), and RENDERS only
    on a turn where WebBrowser was recently used."""

    @pytest.fixture(autouse=True)
    def _restore_shared_config(self, context):
        # ``Context().config`` is the process-cached singleton, so a mutation here
        # would leak into later tests (and a prior test's mutation into ours).
        # Snapshot the fields this class touches and restore them after each test.
        tc = context.config.context.turn_context
        ts = context.config.tools.tool_search
        saved = (
            list(tc.disabled),
            tc.credential_index,
            ts.enabled,
            list(tc.credential_keys),
            dict(tc.credential_values),
        )
        yield
        tc.disabled = list(saved[0])
        tc.credential_index = saved[1]
        ts.enabled = saved[2]
        tc.credential_keys = list(saved[3])
        tc.credential_values = dict(saved[4])

    @staticmethod
    def _xml_role(context):
        # XML path keeps the deferred-tool menu as a normally-on source to filter.
        schema = RoleSchema(
            name="X",
            tools=["Read", "WebBrowser"],
            deferred_tools=["WebBrowser"],
            command_protocol="xml",
        )
        return Role(name="X", role_schema=schema, wiring=AgentWiring.for_context(context))

    @staticmethod
    def _wb_role(context, *, tools=("Read", "WebBrowser")):
        return Role(
            name="X",
            role_schema=RoleSchema(name="X", tools=list(tools)),
            wiring=AgentWiring.for_context(context),
        )

    @staticmethod
    def _enable_secrets(role, tmp_path, monkeypatch):
        # Redirect the key file + vault into tmp and skip config discovery so the
        # test never touches the real ~/.mote or harvests a real config.yaml.
        from mote.runtime.agent.runtime_modules import integrations

        monkeypatch.setattr("mote.runtime.secrets.cipher.CONFIG_ROOT", tmp_path)
        monkeypatch.setattr(integrations, "_primary_config_path", lambda cwd: None)
        role.config.secrets.vault_path = str(tmp_path / "vault.json")
        role.config.secrets.secrets_config_path = str(tmp_path / "secrets_config.json")

    # -- opt-out blacklist -----------------------------------------------------
    def test_disabled_blacklist_suppresses_named_source(self, context):
        r = self._xml_role(context)
        r.config.context.turn_context.disabled = ["deferred_tool_index"]
        assert r.turn_context_source("deferred_tool_index") is None

    def test_disabled_blacklist_leaves_other_sources(self, context):
        # Blacklisting an unrelated name does not drop the menu. Pin tool_search on
        # so the deferred menu is present regardless of prior shared-config state.
        r = self._xml_role(context)
        r.config.tools.tool_search.enabled = True
        r.config.context.turn_context.disabled = ["some_other_source"]
        assert r.turn_context_source("deferred_tool_index") is not None

    # -- credential index (opt-in, three gates) --------------------------------
    def test_credential_index_default_off(self, context):
        # Opt-in: the schema default is off, and a role with it off wires nothing
        # even when WebBrowser-equipped. (Assert the SCHEMA default directly — the
        # ambient user config may enable it, so pin the role's toggle off here.)
        from mote.contracts.config.context import TurnContextConfig

        assert TurnContextConfig().credential_index is False
        r = self._wb_role(context)
        r.config.context.turn_context.credential_index = False
        assert r.turn_context_source("credential_index") is None

    def test_credential_index_off_without_webbrowser(self, context, tmp_path, monkeypatch):
        # Toggle on + secrets on, but the role does not declare WebBrowser → not wired.
        r = self._wb_role(context, tools=["Read"])
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        assert r.turn_context_source("credential_index") is None

    def test_credential_index_wires_without_a_disableable_secret_gate(self, context):
        r = self._wb_role(context)
        r.config.context.turn_context.credential_index = True
        assert r.turn_context_source("credential_index") is not None

    def test_credential_index_wired_when_all_gates_pass(self, context, tmp_path, monkeypatch):
        r = self._wb_role(context)
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        src = r.turn_context_source("credential_index")
        assert src is not None
        assert src.name == "credential_index"
        assert src.save_to_context is False

    # -- credential index (dynamic render gate: recent WebBrowser use) ---------
    def test_credential_index_silent_without_recent_browser_use(self, context, tmp_path, monkeypatch):
        # Constructed (all gates pass) but no WebBrowser call in history → the
        # render gate self-suppresses even though a secret is configured.
        r = self._wb_role(context)
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        r._components.secret_store.add_user_secret("gh_token", "ghp_uservalue123")
        src = r.turn_context_source("credential_index")
        assert asyncio.run(src.render()) is None

    def test_credential_index_renders_after_recent_browser_use(self, context, tmp_path, monkeypatch):
        # A WebBrowser tool call in the recent tail flips the render gate on.
        r = self._wb_role(context)
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        r.config.context.turn_context.credential_keys = []  # ambient config may whitelist
        r._components.secret_store.add_user_secret("gh_token", "ghp_uservalue123")
        self._mark_browser_used(r)
        out = asyncio.run(r.turn_context_source("credential_index").render())
        assert out is not None
        assert "- gh_token: <agent-vault:gh_token>" in out

    @staticmethod
    def _mark_browser_used(role):
        # Put a WebBrowser tool call in the recent tail so the render gate opens.
        role.context_manager.messages.append(
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "WebBrowser", "args": {}}])
        )

    # -- credential index (config: key whitelist + inline non-secret values) ---
    def test_credential_keys_whitelist_narrows_to_listed(self, context, tmp_path, monkeypatch):
        # A non-empty whitelist exposes ONLY the listed secret; others drop out.
        r = self._wb_role(context)
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        r._components.secret_store.add_user_secret("gh_token", "ghp_x")
        r._components.secret_store.add_user_secret("aws_key", "akia_y")
        r.config.context.turn_context.credential_keys = ["gh_token"]
        self._mark_browser_used(r)
        out = asyncio.run(r.turn_context_source("credential_index").render())
        assert out is not None
        assert "- gh_token: <agent-vault:gh_token>" in out
        assert "aws_key" not in out

    def test_credential_keys_empty_exposes_all(self, context, tmp_path, monkeypatch):
        # Empty whitelist (default) exposes every configured secret.
        r = self._wb_role(context)
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        r._components.secret_store.add_user_secret("gh_token", "ghp_x")
        r._components.secret_store.add_user_secret("aws_key", "akia_y")
        r.config.context.turn_context.credential_keys = []
        self._mark_browser_used(r)
        out = asyncio.run(r.turn_context_source("credential_index").render())
        assert out is not None
        assert "gh_token" in out and "aws_key" in out

    def test_inline_non_secret_values_render_literally(self, context, tmp_path, monkeypatch):
        # Inline name:value pairs are non-secrets → shown as their literal value
        # (no placeholder, not stored in the vault), merged alongside secrets.
        r = self._wb_role(context)
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        r.config.context.turn_context.credential_keys = []  # ambient config may whitelist
        r._components.secret_store.add_user_secret("gh_token", "ghp_x")
        r.config.context.turn_context.credential_values = {"username": "alice"}
        self._mark_browser_used(r)
        out = asyncio.run(r.turn_context_source("credential_index").render())
        assert out is not None
        assert "- username: alice" in out  # literal, no placeholder
        assert "- gh_token: <agent-vault:gh_token>" in out

    def test_secret_placeholder_wins_over_inline_on_collision(self, context, tmp_path, monkeypatch):
        # A real vault secret must never be shadowed by an inline plaintext of the
        # same name — the placeholder takes precedence.
        r = self._wb_role(context)
        self._enable_secrets(r, tmp_path, monkeypatch)
        r.config.context.turn_context.credential_index = True
        r.config.context.turn_context.credential_keys = []  # ambient config may whitelist
        r._components.secret_store.add_user_secret("token", "real_secret_val")
        r.config.context.turn_context.credential_values = {"token": "PLAINTEXT_LEAK"}
        self._mark_browser_used(r)
        out = asyncio.run(r.turn_context_source("credential_index").render())
        assert out is not None
        assert "- token: <agent-vault:token>" in out
        assert "PLAINTEXT_LEAK" not in out


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
# Compaction-notice feed (explicit rebuild projection + turn-context source)
# =============================================================================
class TestCompactionNotice:
    def test_roster_slot_starts_none(self):
        # No feed has a private slot anymore; the whole roster is built once.
        assert Role(name="X")._components._graph.is_built("turn_context_sources") is False

    def test_lookup_by_name_and_cached(self, role):
        notice = role.turn_context_source("compaction")
        assert notice is not None
        assert role.turn_context_source("compaction") is notice  # cached roster

    def test_not_registered_with_telemetry(self, role):
        assert role.turn_context_source("compaction") not in _wired_subscribers(role)

    def test_rebuild_projection_is_owned_by_turn_context_bus(self, role):
        notice = role.turn_context_source("compaction")
        assert notice in role.turn_context_bus._sources
        assert callable(notice.on_model_context_rebuilt)


# =============================================================================
# File-watch service wiring (opt-in producer and cleanup)
# =============================================================================
class TestFileWatchService:
    def test_slot_starts_none(self):
        assert Role(name="X")._components._graph.is_built("file_watch_service") is False

    def test_none_without_config(self, role):
        # No file_watch config => watcher disabled.
        assert role.file_watch_service is None

    def test_built_when_enabled_without_hook_layer(self, role):
        from mote.contracts.settings.watching import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        svc = role.file_watch_service
        assert svc is not None
        assert svc not in _wired_subscribers(role)

    def test_built_when_enabled_with_hook_layer(self, role):
        from mote.contracts.settings.watching import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        role.register_hook("FileChanged", lambda hook_input: None)
        svc = role.file_watch_service
        assert svc is not None
        assert role.file_watch_service is svc  # cached
        # The service only produces typed events; it is not a telemetry handler.
        assert svc not in _wired_subscribers(role)

    def test_cleanup_stops_and_unsubscribes(self, role):
        from mote.contracts.settings.watching import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True)
        role.register_hook("FileChanged", lambda hook_input: None)

        async def scenario():
            svc = role.file_watch_service

            async def empty_snapshot():
                return {}

            svc.watcher._snapshot_async = empty_snapshot
            await svc.start_async()
            assert svc.watcher.is_running() is True
            await role.cleanup()
            return svc

        svc = asyncio.run(scenario())
        assert svc.watcher.is_running() is False
        assert svc not in _wired_subscribers(role)

    def test_cleanup_safe_when_watcher_never_built(self, role):
        # Nothing to stop — cleanup short-circuits without error.
        asyncio.run(role.cleanup())
        assert role._components._graph.is_built("file_watch_service") is False


class TestFileWatchHotReload:
    """The reload_skills / reload_config flags auto-wire FileChanged handlers."""

    def test_reload_skills_engages_hook_and_watches_skill_dir(self, role):
        from mote.contracts.settings.watching import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        # No manual hook registered: the auto-registered skill handler is what
        # engages the hook layer, so the service builds.
        svc = role.file_watch_service
        assert svc is not None
        skill_root = role._components.skill_manager.source_dirs()[0]
        assert os.path.abspath(skill_root) in svc.watcher._roots

    def test_skill_filechanged_fires_reload(self, role, monkeypatch):
        from mote.contracts.settings.watching import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        _ = role.file_watch_service  # builds + registers the handler
        calls = {"n": 0}

        def fake_reload():
            calls["n"] += 1
            return True

        monkeypatch.setattr(role._components.skill_manager, "reload", fake_reload)

        async def scenario():
            await role.hook_manager.fire(
                "FileChanged",
                {"path": "/proj/skills/demo/SKILL.md", "change_type": "modified"},
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
        from mote.contracts.settings.watching import FileWatchConfig

        monkeypatch.setattr(
            "mote.runtime.agent.runtime_modules.watching.find_git_root",
            lambda cwd: "/proj/repo",
        )
        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        svc = role.file_watch_service
        assert os.path.abspath("/proj/repo") in svc.watcher._roots

    def test_base_root_omitted_outside_a_git_repo(self, role, monkeypatch):
        """Outside a repo the cwd is NOT added — no whole-tree walk from home."""
        from mote.contracts.settings.watching import FileWatchConfig

        monkeypatch.setattr(
            "mote.runtime.agent.runtime_modules.watching.find_git_root",
            lambda cwd: None,
        )
        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_skills=True)
        svc = role.file_watch_service
        # The cwd is never pulled in as a root when there is no repo boundary.
        cwd = os.path.abspath(role.get_cwd())
        assert cwd not in svc.watcher._roots
        # Hot-reload still works: the skill dir is present.
        skill_root = role._components.skill_manager.source_dirs()[0]
        assert os.path.abspath(skill_root) in svc.watcher._roots

    def test_reload_config_engages_hook_and_swaps_config(self, role, monkeypatch):
        from mote.contracts.settings.watching import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_config=True)
        svc = role.file_watch_service
        assert svc is not None  # config handler alone engaged the hook layer

        sentinel = object()
        monkeypatch.setattr(
            "mote.runtime.agent.runtime_maintenance.load_config",
            lambda *a, **k: sentinel,
        )

        async def scenario():
            await role.hook_manager.fire(
                "FileChanged",
                {"path": "/proj/mote/config.yaml", "change_type": "modified"},
            )

        asyncio.run(scenario())
        assert role.config is sentinel

    def test_reload_mcp_watches_files_not_the_mote_dir(self, role):
        """reload_mcp watches the mcp.json FILES, never the whole .mote dir.

        Regression: watching ``<cwd>/.mote`` recursively pulled the agent's own
        ``logs/`` into the baseline, so each debug-log write was detected as a
        FileChanged and logged again — a self-amplifying poll->log->poll storm
        that eventually killed the CLI. Every watched root here must be an
        ``mcp.json`` file (a bare directory root would recurse).
        """
        from mote.contracts.settings.watching import FileWatchConfig

        role.role_schema.file_watch = FileWatchConfig(enabled=True, reload_mcp=True)
        svc = role.file_watch_service
        assert svc is not None
        cwd = os.path.abspath(role.get_cwd())
        # The bare .mote dir is NEVER a root (that is the feedback-loop bug).
        assert os.path.join(cwd, ".mote") not in svc.watcher._roots
        # The cwd-local mcp.json file IS watched, even before it exists.
        assert os.path.join(cwd, ".mote", "mcp.json") in svc.watcher._roots
        # Every root the reload added is an mcp.json file, not a directory.
        mcp_roots = [r for r in svc.watcher._roots if r.endswith("mcp.json")]
        assert mcp_roots  # at least the user + cwd files
        assert all(not os.path.isdir(r) for r in mcp_roots)


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
        from mote.contracts.output import CommittedOutput, RunResult, TranscriptRef

        committed = CommittedOutput("candidate", "mote.text@1", "sha", "  child summary  ")
        return RunResult(
            output="  child summary  ",
            output_record=committed,
            transcript=TranscriptRef(session_id=self.session_id),
        )


class _ForkHandle:
    """Inline handle: runs the spawned fork child to completion, tears it down."""

    def __init__(self, role):
        self.runtime = types.SimpleNamespace(role=role)
        self._role = role

    async def run_to_completion(self, message):
        try:
            return await self._role.run(with_message=message)
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
        from mote.contracts.spawn import SpawnContext

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
            "capture_file_snapshot",
            "observe_file_snapshot",
            "read_file_view",
            "search_files",
            "plan_file_edit",
            "commit_edit_plan",
            "commit_generated_files",
            "record_file_glimpsed",
            "is_resource_visible",
            "get_runtime_host",
            "get_artifact_store",
            "get_artifact_publisher",
            "handoff_runtime",
            "get_browser_stealth",
            "get_browser_locale",
            "get_browser_proxy",
            "get_browser_cdp_endpoint",
            "get_browser_profile",
            "load_browser_profile",
            "save_browser_profile",
            "get_browser_client_certs",
            "get_secret",
            "wait_interruptible",
            "get_bg_pool",
            "get_skill_pool",
            "build_child_agent",
            "run_skill_fork",
            "register_resource",
            "register_task_result",
            "retire_task_result",
            "get_sandbox_runtime",
            "get_device_config",
            "dispatch_tool",
            "list_tool_names",
            "list_graph_tool_names",
            "list_graph_excluded_tool_names",
            "commit_graph_output",
            "resume_graph_output",
            "has_graph_output_restore",
            "graph_run_lease",
            "list_deferred_tools",
            "reveal_tools",
            "describe_deferred_tools",
            "describe_image",
            "invoke_service",
            "get_default_model",
        }

    def test_capability_values_are_bound_methods(self):
        r = Role(name="X")
        caps = r.tool_capabilities()
        assert caps["get_cwd"]() == r.get_cwd()
        # Bound methods compare equal (same instance + same function).
        assert caps["set_cwd"] == r.set_cwd

    @pytest.mark.asyncio
    async def test_service_identity_is_stable_per_tool_call_and_agent(self):
        class RecordingGateway:
            def __init__(self):
                self.invocations = []

            def supports_route(self, route_id, capability):
                return True

            async def execute(self, invocation):
                self.invocations.append(invocation)
                return ResolvedServiceResponse(
                    response=ServiceResponse(value="ok"),
                    endpoint_id="endpoint",
                    endpoint_fingerprint="fingerprint",
                    credential_slot_id="slot",
                    tenant_fingerprint="tenant",
                    service_call_id=invocation.service_call_id,
                    successful_attempt_id="attempt",
                )

            async def resume(self, invocation):
                return await self.execute(invocation)

            async def cancel(self, service_call_id):
                return False

        gateway = RecordingGateway()
        context = types.SimpleNamespace(service_gateway=gateway, aclose=lambda: None)
        left = Role(name="left", wiring=AgentWiring.for_context(context))
        right = Role(name="right", wiring=AgentWiring.for_context(context))
        kwargs = {
            "route_id": "media.image",
            "capability": "media.generate.image",
            "operation_key": "image:0",
            "payload": {"item": {"filename": "a.png"}},
            "semantics": ServiceExecutionSemantics.IDEMPOTENT,
        }

        with bind_tool_call_id("same-provider-call-id"):
            await left.invoke_service(**kwargs)
        with bind_tool_call_id("same-provider-call-id"):
            await right.invoke_service(**kwargs)
        with bind_tool_call_id("same-provider-call-id"):
            await left.invoke_service(**kwargs)

        first, second, replay = gateway.invocations
        assert first.service_call_id == replay.service_call_id
        assert first.idempotency_key == replay.idempotency_key
        assert first.service_call_id != second.service_call_id

    def test_child_agent_construction_requires_an_injected_factory(self):
        build_child_agent = Role(name="X").tool_capabilities()["build_child_agent"]

        with pytest.raises(RuntimeError, match="AgentDependencies.agent_factory"):
            build_child_agent(dict)

    def test_child_agent_construction_uses_the_injected_factory(self):
        class RecordingFactory:
            def __init__(self):
                self.calls = []

            def build(self, agent_cls, /, **kwargs):
                self.calls.append((agent_cls, kwargs))
                return agent_cls(**kwargs)

        class Child:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        factory = RecordingFactory()
        dependencies = AgentDependencies(
            deps=None,
            output_contract=text_output_contract(),
            agent_factory=factory,
        )
        role = Role(wiring=AgentWiring.for_dependencies(dependencies))

        child = role.tool_capabilities()["build_child_agent"](Child, parent_session_id="parent")

        assert child.kwargs == {"parent_session_id": "parent"}
        assert factory.calls == [(Child, {"parent_session_id": "parent"})]


# =============================================================================
# Browser proxy resolution (role_schema override else global config.proxy)
# =============================================================================
class TestBrowserProxy:
    @staticmethod
    def _clear_proxy_env(monkeypatch):
        for var in (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
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
# Browser profile capability (durable encrypted login store)
# =============================================================================
class TestBrowserProfile:
    def test_get_browser_cdp_endpoint_from_schema(self):
        role = Role(
            name="X",
            role_schema=RoleSchema(name="X", browser_cdp_endpoint="http://127.0.0.1:9222"),
        )
        assert role.get_browser_cdp_endpoint() == "http://127.0.0.1:9222"

    def test_get_browser_profile_empty_by_default(self):
        assert Role(name="X").get_browser_profile() == ""

    def test_get_browser_profile_from_schema(self):
        r = Role(name="X", role_schema=RoleSchema(name="X", browser_profile="xhs"))
        assert r.get_browser_profile() == "xhs"

    def test_load_save_delegate_to_store(self, monkeypatch):
        r = Role(name="X")
        calls = []

        class _FakeStore:
            def load(self, name):
                calls.append(("load", name))
                return {"cookies": [name]}

            def save(self, name, state):
                calls.append(("save", name, state))

        monkeypatch.setattr(
            type(r._components),
            "browser_profile_store",
            property(lambda self: _FakeStore()),
        )
        assert r.load_browser_profile("p") == {"cookies": ["p"]}
        r.save_browser_profile("p", {"a": 1})
        assert calls == [("load", "p"), ("save", "p", {"a": 1})]


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
    def test_blocks_without_activity(self):
        # No duration timer: with no event the wait never returns on its own.
        r = Role(name="X")

        async def scenario():
            task = asyncio.create_task(r.wait_interruptible())
            await asyncio.sleep(0.1)
            assert not task.done()
            task.cancel()

        asyncio.run(scenario())

    def test_woken_by_message(self):
        r = Role(name="X")

        async def scenario():
            task = asyncio.create_task(r.wait_interruptible())
            await asyncio.sleep(0.05)
            r.put_message(Message(content="wake"))
            return await asyncio.wait_for(task, timeout=2.0)

        slept = asyncio.run(scenario())
        # `slept` is wall-clock derived (time.time); only assert it's a float,
        # not non-negative, since the wall clock can skew backward (e.g. WSL2).
        assert isinstance(slept, float)

    def test_bounded_wait_wakes_on_deadline(self, context):
        # A positive duration caps the wait: with no event, it returns when the
        # deadline elapses rather than blocking forever.
        r = Role(name="X", wiring=AgentWiring.for_context(context))

        async def scenario():
            return await asyncio.wait_for(r.wait_interruptible(0.1), timeout=2.0)

        slept = asyncio.run(scenario())
        assert isinstance(slept, float)

    def test_bounded_wait_journals_durable_timer(self, context):
        # A bounded wait opens+closes a durable timer in the run journal so a
        # crash-resume could continue by remaining time. After it returns the
        # timer's terminal is recorded (its countdown is over).
        from mote.runtime.ledger import COMPLETED, KIND_TIMER

        r = Role(name="X", wiring=AgentWiring.for_context(context))
        journal = r.executor.journal
        assert journal is not None  # durable config enabled by default

        async def scenario():
            return await asyncio.wait_for(r.wait_interruptible(0.1), timeout=2.0)

        asyncio.run(scenario())
        timers = [rec for rec in journal.records() if rec.kind == KIND_TIMER]
        assert len(timers) == 1
        assert timers[0].status == COMPLETED


# =============================================================================
# end_session
# =============================================================================
class TestEndSession:
    def test_deactivates_and_returns_empty(self):
        # end_session no longer generates a summary — it just deactivates so the
        # run loop terminates. The terminal reply is captured into
        # Successful child output is returned through RunResult, not RoleState.
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
        assert dedupe_tools(["Bash"]) == ["Bash"]

    def test_bash_and_terminal_coexist(self):
        assert dedupe_tools(["Bash", "Terminal"]) == ["Bash", "Terminal"]

    def test_non_shell_tools_untouched(self):
        assert dedupe_tools(["Read", "Write"]) == ["Read", "Write"]

    def test_duplicates_removed(self):
        assert dedupe_tools(["Terminal", "Bash", "Terminal"]) == ["Terminal", "Bash"]

    def test_order_preserved(self):
        assert dedupe_tools(["Read", "Bash", "Write"]) == [
            "Read",
            "Bash",
            "Write",
        ]


# =============================================================================
# Auto-continue decision application
# =============================================================================
class TestAutoContinue:
    def test_continue_enqueues_context(self):
        r = Role(name="A")
        decision = RunCompletionDecision(
            continue_run=True,
            additional_context=("keep working",),
        )
        assert r._apply_continuation_decision(decision) is True
        assert not r.state.msg_buffer.empty()
        msg = r.state.msg_buffer.pop()
        assert "keep working" in msg.content

    def test_complete_does_not_enqueue_context(self):
        r = Role(name="A")
        decision = RunCompletionDecision(
            continue_run=False,
            additional_context=("must not enqueue",),
        )
        assert r._apply_continuation_decision(decision) is False
        assert r.state.msg_buffer.empty()

    def test_continue_without_context_does_not_enqueue(self):
        r = Role(name="A")
        decision = RunCompletionDecision(continue_run=True)
        assert r._apply_continuation_decision(decision) is True
        assert r.state.msg_buffer.empty()

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
        from mote.contracts.settings.hooks import HookConfig
        from mote.contracts.settings.lsp import LspConfig, LspServerConfig
        from mote.contracts.settings.permissions import PermissionConfig
        from mote.contracts.settings.sandbox import SandboxRuntimeConfig
        from mote.contracts.settings.watching import FileWatchConfig

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
        from mote.orchestration.tasks import build_background_task_pool

        return Role(
            role_schema=schema,
            wiring=AgentWiring.for_context(
                context,
                dependencies=AgentDependencies(
                    deps=None,
                    output_contract=text_output_contract(),
                    background_task_pool_builder=build_background_task_pool,
                ),
            ),
        )

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
        for name in (
            "hook_manager",
            "lsp_service",
            "diagnostics_buffer",
            "sandbox_runtime",
            "file_watch_service",
        ):
            assert graph.peek(name) is not None, f"opt-in layer {name!r} was not built"


# =============================================================================
# Tool-execution-scope config wiring (tools.result_limit / tools.effect_ledger
# reach the executor; disabling the ledger yields no ledger)
# =============================================================================
class TestToolExecConfigWiring:
    """The ``tools`` config group is the single source for the two tool-exec
    policies the executor owns. ``_build_executor`` reads them off
    ``role.config.tools`` and threads them in, so a YAML override reaches the
    live executor (and the spill reducer borrows the same ``result_limit``
    instance back off the executor — proven here by identity)."""

    def test_result_limit_and_ledger_config_reach_executor(self, context):
        role = Role(
            name="X",
            wiring=AgentWiring.for_context(context),
            role_schema=RoleSchema(tools=["Read"]),
        )
        # Override before the (lazy) executor is built.
        role.config.tools.result_limit.default_max_result_size_chars = 12345
        role.config.tools.effect_ledger.enabled = True

        ex = role.executor
        # The executor exposes the two configs it owns; both are the very
        # instances configured under ``tools`` (identity, not just value).
        assert ex.limit_config is role.config.tools.result_limit
        assert ex.limit_config.default_max_result_size_chars == 12345
        assert ex.ledger_config is role.config.tools.effect_ledger
        assert ex.ledger is not None  # enabled -> a real ledger is built

    def test_ledger_disabled_yields_no_ledger(self, context):
        role = Role(
            name="X",
            wiring=AgentWiring.for_context(context),
            role_schema=RoleSchema(tools=["Read"]),
        )
        role.config.tools.effect_ledger.enabled = False
        assert role.executor.ledger is None  # disabled -> run_command skips all ledger work

    def test_spill_reducer_borrows_the_executor_result_limit(self, context):
        # Zero-drift proof: the compaction spill reducer does not build its own
        # ToolResultLimitConfig — it reuses the one the executor owns, which is
        # the one configured under ``tools``.
        role = Role(
            name="X",
            wiring=AgentWiring.for_context(context),
            role_schema=RoleSchema(tools=["Read"]),
        )
        cm = role._components._graph.get("context_manager")
        assert cm._spill._limit is role.config.tools.result_limit
