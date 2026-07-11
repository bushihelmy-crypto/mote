#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the ResourceReporter -> bus -> ReporterSubscriber unification.

The reporter no longer POSTs directly; it emits a :class:`ResourceReportEvent`,
and the :class:`ReporterSubscriber` (when wired) reconstructs the legacy
``_format_data`` payload and POSTs it (best-effort). These tests pin down:

* the rebuilt payload matches the old ``_format_data`` (value normalization,
  ``"path"`` absolutization, role/extra carry-through);
* sync (``handle_sync``) and async (``handle``) paths POST the same payload;
* an empty callback url never POSTs, and POST errors are swallowed;
* ``async_report`` under a bound bus emits the event (with the role) and the
  async CM exit emits the END_MARKER.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import mote.common.utils.report as report_mod
from mote.common.events import EventBus, ResourceReportEvent, set_bus
from mote.common.interface.event_subscriber import ObservationSubscriber, SyncObserver
from mote.common.utils.report import (
    CURRENT_ROLE,
    END_MARKER_NAME,
    BlockType,
    ReporterSubscriber,
    ThoughtReporter,
    _build_report_payload,
)


class _Recorder(ObservationSubscriber, SyncObserver):
    priority = 50

    def __init__(self):
        self.events = []

    def handle_sync(self, event):
        if isinstance(event, ResourceReportEvent):
            self.events.append(event)

    async def handle(self, event):
        if isinstance(event, ResourceReportEvent):
            self.events.append(event)
        return None


class _Val(BaseModel):
    a: int = 1


# ---------------------------------------------------------------------------
# Payload reconstruction parity with the legacy _format_data
# ---------------------------------------------------------------------------


def _legacy_payload(reporter, value, name, extra):
    """Old _format_data output minus the vestigial ``enable_llm_stream`` key."""
    data = reporter._format_data(value, name, extra)
    data.pop("enable_llm_stream", None)
    return data


def test_payload_matches_legacy_for_basemodel(monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "MOTE_ROLE": "dev"})
    reporter = ThoughtReporter()
    value = _Val(a=7)
    event = ResourceReportEvent(
        block=reporter.block.value,
        name_="object",
        value=value,
        extra=None,
        uuid=str(reporter.uuid),
        role="dev",
    )
    assert _build_report_payload(event) == _legacy_payload(reporter, value, "object", None)


def test_payload_absolutizes_path(monkeypatch):
    reporter = ThoughtReporter()
    event = ResourceReportEvent(
        block=reporter.block.value, name_="path", value="rel/file.txt", uuid=str(reporter.uuid), role="r"
    )
    data = _build_report_payload(event)
    assert data["value"] == os.path.abspath("rel/file.txt")
    assert data["name"] == "path"


def test_payload_normalizes_pathlib_and_carries_extra():
    event = ResourceReportEvent(
        block="Docs", name_="content", value=Path("/tmp/x"), extra={"k": "v"}, uuid="u", role="r"
    )
    data = _build_report_payload(event)
    assert data["value"] == "/tmp/x"
    assert data["extra"] == {"k": "v"}
    assert data == {
        "block": "Docs",
        "uuid": "u",
        "value": "/tmp/x",
        "name": "content",
        "role": "r",
        "extra": {"k": "v"},
    }


# ---------------------------------------------------------------------------
# ReporterSubscriber HTTP push (sync + async), parity and error handling
# ---------------------------------------------------------------------------


def test_sync_handle_posts_payload(monkeypatch):
    posted = []
    monkeypatch.setattr(report_mod.requests, "post", lambda url, json=None: posted.append((url, json)))
    sub = ReporterSubscriber("http://ui/report")
    event = ResourceReportEvent(block="Terminal", name_="content", value="hi", uuid="u", role="r")
    sub.handle_sync(event)
    assert posted == [("http://ui/report", _build_report_payload(event))]


@pytest.mark.asyncio
async def test_async_handle_posts_same_payload(monkeypatch):
    posted = []
    monkeypatch.setattr(report_mod, "ClientSession", _make_fake_session(posted))
    sub = ReporterSubscriber("http://ui/report")
    event = ResourceReportEvent(block="Terminal", name_="content", value="hi", uuid="u", role="r")
    out = await sub.handle(event)
    assert out is None
    assert posted == [("http://ui/report", _build_report_payload(event))]


def test_empty_url_never_posts(monkeypatch):
    posted = []
    monkeypatch.setattr(report_mod.requests, "post", lambda *a, **k: posted.append(1))
    ReporterSubscriber("").handle_sync(ResourceReportEvent(block="Terminal", name_="content"))
    assert posted == []


def test_sync_post_error_is_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(report_mod.requests, "post", _boom)
    # Must not raise.
    ReporterSubscriber("http://ui").handle_sync(ResourceReportEvent(block="Terminal", name_="content"))


@pytest.mark.asyncio
async def test_async_post_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(report_mod, "ClientSession", _make_fake_session(None, raise_on_post=True))
    out = await ReporterSubscriber("http://ui").handle(ResourceReportEvent(block="Terminal", name_="content"))
    assert out is None


def test_non_resource_event_ignored(monkeypatch):
    posted = []
    monkeypatch.setattr(report_mod.requests, "post", lambda *a, **k: posted.append(1))
    ReporterSubscriber("http://ui").handle_sync(SimpleNamespace(name="other"))
    assert posted == []


# ---------------------------------------------------------------------------
# ResourceReporter emits onto the bus (producer side)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_report_emits_event_with_role():
    bus = EventBus()
    rec = _Recorder()
    bus.subscribe(rec)
    reporter = ThoughtReporter()
    token = CURRENT_ROLE.set(SimpleNamespace(name="alice"))
    try:
        with set_bus(bus):
            await reporter.async_report({"x": 1}, "object")
    finally:
        CURRENT_ROLE.reset(token)
    assert len(rec.events) == 1
    e = rec.events[0]
    assert e.block == BlockType.THOUGHT.value and e.name_ == "object" and e.role == "alice"
    assert e.value == {"x": 1}


def test_sync_report_emits_event():
    bus = EventBus()
    rec = _Recorder()
    bus.subscribe(rec)
    reporter = ThoughtReporter()
    with set_bus(bus):
        reporter.report({"y": 2}, "object")
    assert len(rec.events) == 1 and rec.events[0].name_ == "object"


@pytest.mark.asyncio
async def test_async_cm_exit_emits_end_marker():
    bus = EventBus()
    rec = _Recorder()
    bus.subscribe(rec)
    with set_bus(bus):
        async with ThoughtReporter():
            pass
    # The CM exit reports the END_MARKER as a final event.
    assert any(e.name_ == END_MARKER_NAME for e in rec.events)


def test_report_no_op_without_bus():
    # Standalone (no bus bound): emitting is a no-op, no POST, no error.
    ThoughtReporter().report({"z": 3}, "object")


# ---------------------------------------------------------------------------
# Fake aiohttp ClientSession
# ---------------------------------------------------------------------------


def _make_fake_session(posted, *, raise_on_post=False):
    class _Resp:
        def __init__(self, url, json):
            if raise_on_post:
                raise RuntimeError("network down")
            if posted is not None:
                posted.append((url, json))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return "ok"

    class _Session:
        def __init__(self, **kwargs):
            self._kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None):
            return _Resp(url, json)

    return _Session
