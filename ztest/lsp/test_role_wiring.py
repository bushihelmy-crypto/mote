#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Role LSP wiring: opt-in gate, telemetry registration, and cleanup."""
from __future__ import annotations

import asyncio
import sys

from mote.contracts.settings.lsp import LspConfig, LspServerConfig
from mote.runtime.agent.role import Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.wiring import AgentWiring
from mote.runtime.models.clients.context import Context


def _lsp_config(enabled=True, servers=True):
    srv = [LspServerConfig(name="fake", command=[sys.executable, "-c", ""], extensions=[".py"])] if servers else []
    return LspConfig(enabled=enabled, servers=srv)


def test_lsp_service_none_when_unconfigured():
    r = Role(name="X")
    assert r.role_schema.lsp is None
    assert r.lsp_service is None


def test_lsp_service_none_when_disabled():
    schema = RoleSchema(name="X", lsp=_lsp_config(enabled=False))
    r = Role(role_schema=schema)
    assert r.lsp_service is None


def test_lsp_service_none_when_no_servers():
    schema = RoleSchema(name="X", lsp=_lsp_config(servers=False))
    r = Role(role_schema=schema)
    assert r.lsp_service is None


def test_lsp_service_built_when_configured():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema)
    svc = r.lsp_service
    assert svc is not None
    # Cached on the slot.
    assert r.lsp_service is svc
    assert r._components._graph.peek("lsp_service") is svc


def test_slot_starts_none():
    r = Role(name="X")
    assert r._components._graph.peek("lsp_service") is None


def test_lsp_service_registered_with_telemetry_when_configured():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, wiring=AgentWiring.for_context(Context()))

    async def scenario():
        await r._components._wire_telemetry()
        handlers = [worker.binding.handler for worker in r.telemetry._workers.values()]
        assert r.lsp_service in handlers
        assert r.lsp_service._telemetry is r.telemetry
        assert r.diagnostics_buffer in handlers
        await r.telemetry.aclose()

    asyncio.run(scenario())


def test_diagnostics_buffer_none_when_unconfigured():
    r = Role(name="X")
    assert r.role_schema.lsp is None
    assert r.diagnostics_buffer is None


def test_diagnostics_buffer_built_and_cached_when_configured():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema)
    buf = r.diagnostics_buffer
    assert buf is not None
    assert r.diagnostics_buffer is buf  # cached


def test_turn_context_lsp_source_is_the_buffer():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, wiring=AgentWiring.for_context(Context()))
    # The buffer is dual-role: the telemetry handler (input) is
    # the turn-context source (output) — no separate wrapper. Same cached
    # instance on both edges, else the staged blocks would never be rendered.
    lsp_source = next(s for s in r.turn_context_bus._sources if s.name == "lsp")
    assert lsp_source is r.diagnostics_buffer
    assert r.diagnostics_buffer in r._components._build_telemetry_subscribers()


def test_no_lsp_handler_when_unconfigured():
    r = Role(name="X", wiring=AgentWiring.for_context(Context()))
    assert r.lsp_service is None
    assert not any(hasattr(handler, "drain_diagnostics") for handler in r._components._build_telemetry_subscribers())


def test_cleanup_shuts_down_lsp_service():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema)
    svc = r.lsp_service

    called = {"n": 0}
    orig = svc.aclose

    async def _spy():
        called["n"] += 1
        await orig()

    svc.aclose = _spy
    asyncio.run(r.cleanup())
    assert called["n"] == 1


def test_cleanup_safe_when_lsp_never_built():
    r = Role(name="X")
    assert r._components._graph.peek("lsp_service") is None
    asyncio.run(r.cleanup())  # no error, no LSP shutdown attempted
    assert r._components._graph.peek("lsp_service") is None
