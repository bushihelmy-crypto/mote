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
from datetime import datetime, timezone

import pytest

from mote.contracts.events.envelope import EventEnvelope, EventId, EventType, StreamId
from mote.contracts.events.file.facts import FileTransactionCommittedEvent
from mote.contracts.events.telemetry import DiagnosticsEvent
from mote.contracts.file.identity import NameIdentity, PresentVersion, TargetIdentity
from mote.runtime.config.lsp import LspConfig, LspServerConfig
from mote.runtime.lsp.buffer import DiagnosticsBuffer
from mote.runtime.lsp.service import LspService
from mote.ztest.telemetry import InlineTelemetry

aio = pytest.mark.asyncio

_FAKE = os.path.join(os.path.dirname(__file__), "fake_lsp_server.py")


def _committed(path: str, *, sequence: int = 1) -> EventEnvelope:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    version = PresentVersion(
        NameIdentity("name", "test"),
        TargetIdentity("target", "test"),
        1,
        1,
        "0" * 64,
        "1" * 64,
    )
    fact = FileTransactionCommittedEvent(f"tx-{sequence}", (path,), (version,))
    return EventEnvelope(
        EventId(f"event-{sequence}"),
        EventType(FileTransactionCommittedEvent.type),
        1,
        StreamId("session/test"),
        sequence,
        timestamp,
        timestamp,
        fact.payload(),
    )


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
        with pytest.raises(RuntimeError, match="unavailable"):
            await svc.file_saved(str(f))
        # The failed generation remains unavailable and never becomes an empty success.
        with pytest.raises(RuntimeError, match="unavailable"):
            await svc.file_saved(str(f))
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


@aio
async def test_handle_file_mutated_delegates_to_file_saved(tmp_path):
    # As a reliable event handler, the committed FileOps fact routes through handle() to
    # file_saved() — same diagnostics回流 as a direct file_saved call.
    f = tmp_path / "mod.py"
    f.write_text("x = ERROR\n")
    svc = LspService(_config(), str(tmp_path))
    try:
        outcome = await svc.handle(_committed(str(f)))
        assert outcome is None  # observation subscriber returns nothing
        block = svc.drain_diagnostics()
        assert "<lsp_diagnostics>" in block
        assert "fake error token found" in block
    finally:
        await svc.shutdown()


@aio
async def test_handle_ignores_non_file_mutated_events(tmp_path):
    svc = LspService(_config(), str(tmp_path))
    try:
        with pytest.raises(AttributeError):
            await svc.handle(object())  # type: ignore[arg-type]
        assert svc.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


@aio
async def test_handle_ignores_empty_path(tmp_path):
    svc = LspService(_config(), str(tmp_path))
    try:
        with pytest.raises(ValueError):
            await svc.handle(_committed(""))
        assert svc.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


# --- Output side: diagnostics ride telemetry as DiagnosticsEvent -------------


@aio
async def test_handle_emits_diagnostics_event_to_buffer(tmp_path):
    # Service produces DiagnosticsEvent; the buffer accumulates it.
    f = tmp_path / "mod.py"
    f.write_text("x = ERROR\n")
    buffer = DiagnosticsBuffer()
    telemetry = InlineTelemetry()
    svc = LspService(_config(), str(tmp_path), telemetry=telemetry)
    telemetry.handlers.append(buffer)
    try:
        await svc.handle(_committed(str(f)))
        block = buffer.drain_diagnostics()
        assert "<lsp_diagnostics>" in block
        assert "fake error token found" in block
        assert str(f) in block
        # Drain is one-shot.
        assert buffer.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


@aio
async def test_emitted_event_carries_paths(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = ERROR\n")
    seen = []

    class _Spy:
        async def handle(self, event):
            if isinstance(event, DiagnosticsEvent):
                seen.append(event)
            return None

    telemetry = InlineTelemetry(_Spy())
    svc = LspService(_config(), str(tmp_path), telemetry=telemetry)
    try:
        await svc.handle(_committed(str(f)))
        assert len(seen) == 1
        assert seen[0].paths == [str(f)]
        assert seen[0].block
    finally:
        await svc.shutdown()


@aio
async def test_no_emit_without_telemetry(tmp_path):
    # An unbound service stays inert on the output side (diagnostics still drain
    # directly), so direct-call test paths are unaffected.
    f = tmp_path / "mod.py"
    f.write_text("x = ERROR\n")
    svc = LspService(_config(), str(tmp_path))
    try:
        await svc.handle(_committed(str(f)))
        # Nothing emitted; the registry still holds the changed set for a pull.
        assert "fake error token found" in svc.drain_diagnostics()
    finally:
        await svc.shutdown()


@aio
async def test_resolved_diagnostics_flow_through_bus(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = ERROR\n")
    buffer = DiagnosticsBuffer()
    telemetry = InlineTelemetry()
    svc = LspService(_config(), str(tmp_path), telemetry=telemetry)
    telemetry.handlers.append(buffer)
    try:
        await svc.handle(_committed(str(f)))
        assert "fake error token found" in buffer.drain_diagnostics()
        # Fix the file -> server clears -> "resolved" surfaces once via the bus.
        f.write_text("x = 1\n")
        await svc.handle(_committed(str(f), sequence=2))
        block2 = buffer.drain_diagnostics()
        assert "resolved" in block2
        assert buffer.drain_diagnostics() == ""
    finally:
        await svc.shutdown()


def test_buffer_accumulates_multiple_blocks():
    # Unit: several DiagnosticsEvents within a turn join into one drained block.
    import asyncio

    buffer = DiagnosticsBuffer()
    asyncio.run(buffer.handle(DiagnosticsEvent(block="block-A", paths=["a.py"])))
    asyncio.run(buffer.handle(DiagnosticsEvent(block="block-B", paths=["b.py"])))
    out = buffer.drain_diagnostics()
    assert out == "block-A\n\nblock-B"
    assert buffer.drain_diagnostics() == ""


def test_buffer_ignores_non_diagnostics_events():
    import asyncio

    buffer = DiagnosticsBuffer()
    asyncio.run(buffer.handle(object()))
    asyncio.run(buffer.handle(DiagnosticsEvent(block="", paths=[])))  # empty block
    assert buffer.drain_diagnostics() == ""


def test_buffer_is_structural_handler_and_context_source():
    # The buffer plays both sides of the push->pull bridge in one object: it is
    # a telemetry handler AND the turn-context EphemeralContextSource
    # (so the thin LspContextSource wrapper is no longer needed).
    from mote.contracts.ports.conversation.turn_context import EphemeralContextSource

    buffer = DiagnosticsBuffer()
    assert hasattr(buffer, "handle")
    assert isinstance(buffer, EphemeralContextSource)
    assert buffer.name == "lsp" and buffer.priority == 40


def test_buffer_render_drains_once():
    import asyncio

    buffer = DiagnosticsBuffer()
    # Nothing staged -> render self-suppresses.
    assert asyncio.run(buffer.render()) is None
    asyncio.run(buffer.handle(DiagnosticsEvent(block="block-A", paths=["a.py"])))
    assert asyncio.run(buffer.render()) == "block-A"
    # One-shot: cleared after render.
    assert asyncio.run(buffer.render()) is None
