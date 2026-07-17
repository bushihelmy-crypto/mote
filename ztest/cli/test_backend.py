#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :mod:`mote.cli.backend` — the single engine-binding seam.

Two concerns: the **accessors** (pure attribute pokes — verified against
lightweight fakes so we assert the operation, not the engine) and the
**construction** helpers (``build_role`` generic + typed paths, ``turn_message``,
``list_agent_types``) which touch the real engine / agent registry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.cli import backend
from mote.common.const import IMAGES
from mote.executor.agent_registry import registry
from mote.roles import Role
from mote.roles.role_schema import RoleSchema

# --------------------------------------------------------------------------
# Accessors (fakes)
# --------------------------------------------------------------------------


def test_bind_human_channel_sets_env():
    role = SimpleNamespace(state=SimpleNamespace(env=None))
    channel = object()
    backend.bind_human_channel(role, channel)
    assert role.state.env is channel


def test_runtime_name_reads_role_schema():
    runtime = SimpleNamespace(role=SimpleNamespace(role_schema=SimpleNamespace(name="Helper")))
    assert backend.runtime_name(runtime) == "Helper"


def test_runtime_name_falls_back_to_question_mark():
    runtime = SimpleNamespace(role=SimpleNamespace())  # no role_schema
    assert backend.runtime_name(runtime) == "?"


def test_runtime_role_returns_role():
    role = object()
    assert backend.runtime_role(SimpleNamespace(role=role)) is role


def test_role_event_bus_and_cleanup_and_session_id():
    bus, cleanup = object(), object()
    role = SimpleNamespace(session_id="sid-1", event_bus=bus, cleanup=cleanup)
    assert backend.role_session_id(role) == "sid-1"
    assert backend.role_event_bus(role) is bus
    assert backend.role_cleanup(role) is cleanup
    # Missing attributes degrade to None.
    bare = SimpleNamespace(session_id="x")
    assert backend.role_event_bus(bare) is None
    assert backend.role_cleanup(bare) is None


def test_fork_role_without_fork_session_returns_none():
    assert backend.fork_role(SimpleNamespace()) is None


def test_fork_role_swallows_exception():
    def boom():
        raise RuntimeError("no")

    assert backend.fork_role(SimpleNamespace(fork_session=boom)) is None


def test_fork_role_returns_forked():
    forked = object()
    role = SimpleNamespace(fork_session=lambda: forked)
    assert backend.fork_role(role) is forked


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


# --------------------------------------------------------------------------
# Construction (real engine)
# --------------------------------------------------------------------------


def _context():
    return backend.build_context(backend.load_config())


def test_build_role_generic_path_smoke():
    role = backend.build_role(context=_context(), name="Tester", tools=["Read"])
    assert isinstance(role, Role)
    assert role.role_schema.name == "Tester"


# --- MCP discovery + watcher wiring on the generic (top-level) path ---------


def test_discover_mcps_empty_when_unconfigured(monkeypatch):
    # A missing / empty .mote/mcp.json yields no servers (MCP stays off).
    monkeypatch.setattr(backend, "load_mcp_servers", lambda cwd=None: [])
    assert backend._discover_mcps() == []


def test_discover_mcps_names_every_configured_server(monkeypatch):
    from mote.common.config.config.mcp_config import MCPServerConfig, MCPTransportType

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
    role = backend.build_role(context=_context(), name="Tester")
    # Empty config ⇒ empty mcps (nothing to load), but the field is populated
    # from discovery, not left at the schema default by accident.
    assert role.role_schema.mcps == []


def test_generic_role_enables_file_watch_hot_reload():
    role = backend.build_role(context=_context(), name="Tester")
    fw = role.role_schema.file_watch
    # The interactive top-level role watches the workspace so an mcp_config.json
    # or SKILL.md change hot-reloads mid-session.
    assert fw is not None
    assert fw.enabled is True
    assert fw.reload_mcp is True
    assert fw.reload_skills is True


def test_generic_role_uses_curated_default_when_none_passed():
    role = backend.build_role(context=_context(), name="Tester")
    # No explicit tools ⇒ RoleSchema's curated default (its declared tool
    # surface), NOT the full registered toolbox. So the CLI reports exactly the
    # declared set — internal control verbs (End/Reply/Ask/…) that are
    # registered but not curated do not leak into the top-level role.
    assert role.role_schema.tools == RoleSchema.model_fields["tools"].default
    assert "Read" in role.role_schema.tools
    # Skill is now a DEFERRED tool (discovered via SearchTools), so it lives on
    # the deferred surface rather than the always-visible ``tools`` list.
    assert "Skill" in role.role_schema.deferred_tools


def test_generic_role_explicit_tools_are_respected():
    role = backend.build_role(context=_context(), name="Tester", tools=["Read", "Grep"])
    # An explicit list still wins over the empty ⇒ load-all default.
    assert role.role_schema.tools == ["Read", "Grep"]


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
    role = backend.build_role(context=_context(), name="x", agent_type="DefinitelyNotAnAgent")
    assert role is None


def test_build_role_typed_path_returns_registered_instance():
    from mote.common.base.agent import BaseAgent

    class _ThrowawayAgent(BaseAgent, Role):
        agent_name = "ThrowawayAgent"
        description = "A throwaway agent for tests."

    registry.register(_ThrowawayAgent)
    try:
        role = backend.build_role(context=_context(), name="tw", agent_type="ThrowawayAgent")
        assert isinstance(role, _ThrowawayAgent)
    finally:
        registry._registry.pop("ThrowawayAgent", None)


def test_list_agent_types_includes_registered():
    from mote.common.base.agent import BaseAgent

    class _ListedAgent(BaseAgent, Role):
        agent_name = "ListedAgent"
        description = "A listed agent."

    # discover() is idempotent (already ran); register after so it appears.
    registry.register(_ListedAgent)
    try:
        types = backend.list_agent_types()
        assert ("ListedAgent", "A listed agent.") in types
    finally:
        registry._registry.pop("ListedAgent", None)
