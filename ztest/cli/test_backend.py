#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :mod:`mote.product.entrypoints.cli.backend` — the single engine-binding seam.

Two concerns: the **accessors** (pure attribute pokes — verified against
lightweight fakes so we assert the operation, not the engine) and the
**construction** helpers (``build_role`` generic + typed paths, ``turn_message``,
``list_agent_types``) which touch the real engine / agent registry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.contracts.conversation.fields import IMAGES
from mote.product.agents.catalog import AgentCatalog
from mote.product.agents.factory import CodingAgentFactory
from mote.product.composition.agent_factory import build_product_agent
from mote.product.composition.bootstrap import _build_application_context
from mote.product.composition.container import ProductContainer
from mote.product.entrypoints.cli import backend
from mote.product.paths import default_runtime_paths
from mote.runtime.agent import Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.services import EngineServices
from mote.ztest.model_fakes import offline_config

# --------------------------------------------------------------------------
# Accessors (fakes)
# --------------------------------------------------------------------------


def test_bind_human_channel_uses_explicit_binding():
    bound = []
    role = SimpleNamespace(bind_human_interaction=bound.append)
    channel = object()
    backend.bind_human_channel(role, channel)
    assert bound == [channel]


def test_runtime_name_reads_role_schema():
    runtime = SimpleNamespace(role=SimpleNamespace(role_schema=SimpleNamespace(name="Helper")))
    assert backend.runtime_name(runtime) == "Helper"


def test_runtime_name_falls_back_to_question_mark():
    runtime = SimpleNamespace(role=SimpleNamespace())  # no role_schema
    assert backend.runtime_name(runtime) == "?"


def test_runtime_role_returns_role():
    role = object()
    assert backend.runtime_role(SimpleNamespace(role=role)) is role


def test_role_telemetry_and_cleanup_and_session_id():
    telemetry, cleanup = object(), object()
    role = SimpleNamespace(session_id="sid-1", telemetry=telemetry, cleanup=cleanup)
    assert backend.role_session_id(role) == "sid-1"
    assert backend.role_telemetry(role) is telemetry
    assert backend.role_cleanup(role) is cleanup
    # Missing attributes degrade to None.
    bare = SimpleNamespace(session_id="x")
    assert backend.role_telemetry(bare) is None
    assert backend.role_cleanup(bare) is None


@pytest.mark.asyncio
async def test_fork_role_without_fork_session_returns_none():
    assert await backend.fork_role(SimpleNamespace()) is None


@pytest.mark.asyncio
async def test_fork_role_swallows_exception():
    async def boom():
        raise RuntimeError("no")

    assert await backend.fork_role(SimpleNamespace(fork_session=boom)) is None


@pytest.mark.asyncio
async def test_fork_role_returns_forked():
    forked = object()

    async def fork_session():
        return forked

    role = SimpleNamespace(fork_session=fork_session)
    assert await backend.fork_role(role) is forked


class _FakeCM:
    def __init__(self, n):
        self._n = n
        self.cleared = False

    def count(self):
        return self._n

    async def clear(self):
        self.cleared = True


@pytest.mark.asyncio
async def test_clear_messages_counts_then_clears():
    cm = _FakeCM(4)
    role = SimpleNamespace(context_manager=cm)
    assert await backend.clear_messages(role) == 4
    assert cm.cleared is True


@pytest.mark.asyncio
async def test_clear_messages_without_manager_returns_zero():
    assert await backend.clear_messages(SimpleNamespace()) == 0


def test_turn_message_attaches_images_metadata():
    msg = backend.turn_message("hello", ["AAA", "BBB"])
    assert msg.content == "hello"
    assert msg.metadata[IMAGES] == ["AAA", "BBB"]


def test_turn_message_without_images_sets_no_metadata():
    msg = backend.turn_message("plain", None)
    assert IMAGES not in msg.metadata


@pytest.mark.asyncio
async def test_rewind_files_runs_blocking_read_and_mutation_off_loop(monkeypatch, tmp_path):
    calls = []
    target = SimpleNamespace(
        working_dir="/workspace",
        commit="target",
        after_commit="after",
        index=0,
    )

    async def run_disk_io(fn, /, *args, **kwargs):
        calls.append(fn)
        return fn(*args, **kwargs)

    def rewind(**kwargs):
        assert kwargs["target_commit"] == "target"
        return SimpleNamespace(external_paths=("external.txt",))

    monkeypatch.setattr(backend, "run_disk_io", run_disk_io)
    monkeypatch.setattr(backend, "_list_checkpoints", lambda log: [target])
    role = SimpleNamespace(
        state=SimpleNamespace(
            session_id="session",
            project_root="/workspace",
            working_dir="/workspace",
        ),
        context=SimpleNamespace(disk_writer=object()),
        file_operations=SimpleNamespace(rewind=rewind),
        _components=SimpleNamespace(workspace_store=SimpleNamespace(sessions_root=tmp_path)),
    )

    result = await backend.rewind_files(role, 0)

    assert calls == [backend._list_checkpoints, rewind]
    assert result is not None
    assert result.external == ["external.txt"]


# --------------------------------------------------------------------------
# Construction (real engine)
# --------------------------------------------------------------------------


def _context(config=None):
    return _build_application_context(config or offline_config(), paths=default_runtime_paths())


def _services():
    return EngineServices(context=_context())


def _build_role(**kwargs):
    config = offline_config()
    services = EngineServices(context=_context(config))
    container = ProductContainer.standard(config)
    kwargs.setdefault("agent_catalog", container.agents)
    cwd = kwargs.pop("cwd", None)
    tools = kwargs.pop("tools", None)
    return build_product_agent(
        services=services,
        agent_factory=container.agent_factory,
        paths=container.paths,
        source_policy=container.extension_sources,
        cwd=Path(cwd) if cwd else None,
        tools=tuple(tools) if tools else None,
        **kwargs,
    )


def test_build_role_generic_path_smoke():
    role = _build_role(name="Tester", tools=["Read"])
    assert isinstance(role, Role)
    assert role.role_schema.name == "Tester"


# --- MCP discovery + watcher wiring on the generic (top-level) path ---------


def test_discover_mcps_empty_when_unconfigured(monkeypatch):
    # A missing / empty .mote/mcp.json yields no servers (MCP stays off).
    monkeypatch.setattr(backend, "load_mcp_servers", lambda cwd=None: [])
    assert backend._discover_mcps() == []


def test_discover_mcps_names_every_configured_server(monkeypatch):
    from mote.contracts.tool.transport import MCPTransportType
    from mote.runtime.config.mcp import MCPServerConfig

    servers = [
        MCPServerConfig(name="fs", type=MCPTransportType.STDIO, enabled=True, command="npx"),
        MCPServerConfig(name="remote", type=MCPTransportType.SSE, enabled=True, url="https://x/sse"),
    ]
    monkeypatch.setattr(backend, "load_mcp_servers", lambda cwd=None: servers)
    # Mirrors the skill "empty include ⇒ load everything" default: every declared
    # server name flows into the schema so the engine initialises them all.
    assert backend._discover_mcps() == ["fs", "remote"]


def test_generic_role_loads_all_mcps(monkeypatch):
    monkeypatch.setattr(backend, "load_mcp_servers", lambda cwd=None: [])
    role = _build_role(name="Tester")
    # Empty config ⇒ empty mcps (nothing to load), but the field is populated
    # from discovery, not left at the schema default by accident.
    assert role.role_schema.mcps == []


def test_generic_role_enables_file_watch_hot_reload(tmp_path):
    role = _build_role(name="Tester", cwd=str(tmp_path))
    fw = role.role_schema.file_watch
    # The interactive top-level role watches the exact MCP config path and skill
    # roots, rather than triggering FileWatchConfig's whole-worktree fallback.
    assert fw is not None
    assert fw.enabled is True
    assert fw.roots == [str(tmp_path / ".mote" / "mcp.json")]
    assert fw.reload_mcp is True
    assert fw.reload_skills is True


def test_generic_role_hot_reload_does_not_add_git_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mote.runtime.agent.components.watching.find_git_root",
        lambda cwd: "/proj/repo",
    )
    role = _build_role(name="Tester", cwd=str(tmp_path))

    roots = role.file_watch_service.watcher._roots

    assert "/proj/repo" not in roots
    assert str(tmp_path / ".mote" / "mcp.json") in roots


def test_generic_role_uses_curated_default_when_none_passed():
    role = _build_role(name="Tester")
    # No explicit tools ⇒ RoleSchema's curated default (its declared tool
    # surface), NOT the full registered toolbox. So the CLI reports exactly the
    # declared set, not every registered internal control verb.
    assert role.role_schema.tools == RoleSchema.model_fields["tools"].get_default(call_default_factory=True)
    assert role.role_schema.deferred_tools == RoleSchema.model_fields["deferred_tools"].get_default(
        call_default_factory=True
    )
    assert "Handoff" not in role.role_schema.tools
    assert "Handoff" not in role.executor.tool_names()


def test_generic_role_explicit_tools_are_respected():
    role = _build_role(name="Tester", tools=["Read", "Search"])
    # An explicit list still wins over the empty ⇒ load-all default.
    assert role.role_schema.tools == ["Read", "Search"]


def test_role_tool_count_reports_builtin_only():
    # MCP servers are on the schema but not counted — the badge reports only the
    # one-time startup tool load. Deduplicated: three names collapse to two.
    role = SimpleNamespace(role_schema=SimpleNamespace(tools=["Read", "Write", "Read"], mcps=["fs"]))
    assert backend.role_tool_count(role) == 2


def test_role_tool_count_degrades_to_zero():
    assert backend.role_tool_count(SimpleNamespace()) == 0
    assert backend.role_tool_count(SimpleNamespace(role_schema=SimpleNamespace())) == 0


def test_role_deferred_tool_count_reports_deduped_deferred():
    role = SimpleNamespace(role_schema=SimpleNamespace(deferred_tools=["WebBrowser", "Agent", "WebBrowser"]))
    assert backend.role_deferred_tool_count(role) == 2


def test_role_deferred_tool_count_zero_when_search_disabled():
    # The global tool-search master switch off ⇒ no tool is deferred, so the badge
    # reports zero deferred even though the schema declares some.
    role = SimpleNamespace(
        role_schema=SimpleNamespace(deferred_tools=["WebBrowser", "Agent"]),
        config=SimpleNamespace(tools=SimpleNamespace(tool_search=SimpleNamespace(enabled=False))),
    )
    assert backend.role_deferred_tool_count(role) == 0


def test_role_deferred_tool_count_degrades_to_zero():
    assert backend.role_deferred_tool_count(SimpleNamespace()) == 0
    assert backend.role_deferred_tool_count(SimpleNamespace(role_schema=SimpleNamespace())) == 0


def test_build_role_unknown_agent_type_returns_none():
    role = _build_role(name="x", agent_type="DefinitelyNotAnAgent")
    assert role is None


def test_build_role_typed_path_returns_registered_instance():
    from mote.contracts.agent import BaseAgent

    class _ThrowawayAgent(BaseAgent, Role):
        agent_name = "ThrowawayAgent"
        description = "A throwaway agent for tests."

    catalog = AgentCatalog.from_types((_ThrowawayAgent,), CodingAgentFactory())
    role = _build_role(
        name="tw",
        agent_type="ThrowawayAgent",
        agent_catalog=catalog,
    )
    assert isinstance(role, _ThrowawayAgent)


def test_list_agent_types_includes_registered():
    from mote.contracts.agent import BaseAgent

    class _ListedAgent(BaseAgent, Role):
        agent_name = "ListedAgent"
        description = "A listed agent."

    catalog = AgentCatalog.from_types((_ListedAgent,), CodingAgentFactory())
    types = backend.list_agent_types(catalog)
    assert ("ListedAgent", "A listed agent.") in types
