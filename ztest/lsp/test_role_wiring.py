#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Role/executor LSP wiring: the opt-in lazy_service gate + executor injection."""
from __future__ import annotations

import sys

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
    assert r._lsp_service is svc


def test_slot_starts_none():
    r = Role(name="X")
    assert r._lsp_service is None
