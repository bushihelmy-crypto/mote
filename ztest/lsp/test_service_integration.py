#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end LSP tests against the fake stdio server (fake_lsp_server.py).

Exercises LspService -> LspServerManager -> LspServerInstance -> JsonRpcEndpoint
against a real subprocess: lazy launch on first relevant edit, diagnostics回流
(error appears, then resolves), routing by extension, the disabled/no-server
no-ops, and clean shutdown.
"""
from __future__ import annotations

import os
import sys

import pytest

from metagpt.common.schema import LspConfig, LspServerConfig
from metagpt.roles.lsp.service import LspService

aio = pytest.mark.asyncio

_FAKE = os.path.join(os.path.dirname(__file__), "fake_lsp_server.py")


def _config(extensions=(".py",), enabled=True):
    return LspConfig(
        enabled=enabled,
        diagnostics_wait=0.4,
        init_timeout=5.0,
        servers=[
            LspServerConfig(
                name="fake",
                command=[sys.executable, _FAKE],
                extensions=list(extensions),
                language_id="python",
            )
        ],
    )


@aio
async def test_diagnostics_flow_error_then_resolved(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = ERROR\n")  # token triggers a fake diagnostic
    svc = LspService(_config(), str(tmp_path))
    try:
        await svc.file_saved(str(f))
        block = svc.drain_diagnostics()
        assert "<lsp_diagnostics>" in block
        assert "fake error token found" in block
        assert str(f) in block

        # Draining again with no change yields nothing.
        assert svc.drain_diagnostics() == ""

        # Fix the file -> server clears diagnostics -> "resolved" surfaced once.
        f.write_text("x = 1\n")
        await svc.file_saved(str(f))
        block2 = svc.drain_diagnostics()
        assert "resolved" in block2
        assert svc.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


@aio
async def test_no_server_for_extension_is_noop(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("ERROR here\n")
    svc = LspService(_config(extensions=(".py",)), str(tmp_path))
    try:
        await svc.file_saved(str(f))  # .txt -> no server handles it
        assert svc.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


@aio
async def test_disabled_config_is_noop(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("ERROR\n")
    svc = LspService(_config(enabled=False), str(tmp_path))
    try:
        await svc.file_saved(str(f))
        assert svc.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


@aio
async def test_empty_path_is_noop(tmp_path):
    svc = LspService(_config(), str(tmp_path))
    try:
        await svc.file_saved("")
        assert svc.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


@aio
async def test_failed_server_not_retried(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("ERROR\n")
    bad = LspConfig(
        enabled=True,
        diagnostics_wait=0.2,
        init_timeout=2.0,
        servers=[
            LspServerConfig(
                name="broken",
                command=["this-command-does-not-exist-xyz"],
                extensions=[".py"],
            )
        ],
    )
    svc = LspService(bad, str(tmp_path))
    try:
        await svc.file_saved(str(f))  # launch fails -> remembered as dead
        assert svc.drain_diagnostics() == ""
        # Second edit: no crash, still inert.
        await svc.file_saved(str(f))
        assert svc.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


@aio
async def test_shutdown_idempotent(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    svc = LspService(_config(), str(tmp_path))
    await svc.file_saved(str(f))
    await svc.shutdown()
    await svc.shutdown()  # no error on second call
