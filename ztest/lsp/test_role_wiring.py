#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Role LSP wiring: the opt-in lazy_service gate + event-bus subscription + cleanup."""
from __future__ import annotations

import asyncio
import sys

from metagpt.router.llm.context import Context
from metagpt.common.schema import LspConfig, LspServerConfig
from metagpt.roles.role import Role
from metagpt.roles.role_schema import RoleSchema


def _lsp_config(enabled=True, servers=True):
    srv = (
        [LspServerConfig(name="fake", command=[sys.executable, "-c", ""], extensions=[".py"])]
        if servers
        else []
    )
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
    assert r._components._lsp_service is svc


def test_slot_starts_none():
    r = Role(name="X")
    assert r._components._lsp_service is None


def test_lsp_service_subscribed_to_event_bus_when_configured():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema, context=Context())
    bus = r.event_bus
    assert r.lsp_service in bus.subscribers
    # Output side: the service got the bus to emit on + the buffer is subscribed.
    assert r.lsp_service.bus is bus
    assert r.diagnostics_buffer in bus.subscribers


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
    r = Role(role_schema=schema, context=Context())
    # The buffer is dual-role: the very object subscribed to the bus (input) is
    # the turn-context source (output) — no separate wrapper. Same cached
    # instance on both edges, else the staged blocks would never be rendered.
    lsp_source = next(s for s in r.turn_context_bus._sources if s.name == "lsp")
    assert lsp_source is r.diagnostics_buffer
    assert r.diagnostics_buffer in r.event_bus.subscribers


def test_no_lsp_subscriber_when_unconfigured():
    r = Role(name="X", context=Context())
    bus = r.event_bus
    assert r.lsp_service is None
    # No subscriber doubles as an LspService (it would have drain_diagnostics).
    assert not any(hasattr(s, "drain_diagnostics") for s in bus.subscribers)


def test_cleanup_shuts_down_lsp_service():
    schema = RoleSchema(name="X", lsp=_lsp_config())
    r = Role(role_schema=schema)
    svc = r.lsp_service

    called = {"n": 0}
    orig = svc.shutdown

    async def _spy():
        called["n"] += 1
        await orig()

    svc.shutdown = _spy
    asyncio.run(r.cleanup())
    assert called["n"] == 1


def test_cleanup_safe_when_lsp_never_built():
    r = Role(name="X")
    assert r._components._lsp_service is None
    asyncio.run(r.cleanup())  # no error, no LSP shutdown attempted
    assert r._components._lsp_service is None
