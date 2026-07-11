#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.session.browser_state`` — the browser-state recorder.

Covers: a record appends a ``browser_state`` event to the shared rollout log
(open-tab URLs + active index + storage_state); the ``enabled`` gate suppresses
recording (privacy: storage_state may carry cookies); recording is best-effort
and conforms to the ``BrowserStateStore`` protocol; replay collects the latest
browser state (last-write-wins).
"""
from __future__ import annotations

from mote.common.interface import BrowserStateStore
from mote.session.browser_state import BrowserStateRecorder
from mote.session.events import BROWSER_STATE
from mote.session.log import SessionLog
from mote.session.replay import replay


def _recorder(tmp_path, **kw):
    log = SessionLog("browser_sess", base_dir=str(tmp_path))
    return BrowserStateRecorder(log, **kw), log


def test_record_appends_browser_state_event(tmp_path):
    rec, log = _recorder(tmp_path)
    rec.record(
        ["https://a.com", "https://b.com"],
        active=1,
        storage_state={"cookies": [{"name": "sid"}], "origins": []},
        tool="WebBrowser",
    )

    records = list(log.iter_raw())
    assert len(records) == 1
    assert records[0]["type"] == BROWSER_STATE
    payload = records[0]["payload"]
    assert payload["urls"] == ["https://a.com", "https://b.com"]
    assert payload["active"] == 1
    assert payload["storage_state"]["cookies"] == [{"name": "sid"}]
    assert payload["tool"] == "WebBrowser"


def test_disabled_gate_suppresses_recording(tmp_path):
    rec, log = _recorder(tmp_path, enabled=False)
    rec.record(["https://a.com"], storage_state={"cookies": [], "origins": []})
    assert list(log.iter_raw()) == []


def test_record_copies_url_list(tmp_path):
    """Mutating the caller's urls list after record must not affect the event."""
    rec, log = _recorder(tmp_path)
    urls = ["https://a.com"]
    rec.record(urls)
    urls.append("https://b.com")

    payload = list(log.iter_raw())[0]["payload"]
    assert payload["urls"] == ["https://a.com"]


def test_record_allows_none_storage_state(tmp_path):
    rec, log = _recorder(tmp_path)
    rec.record(["https://a.com"], active=0, storage_state=None)
    payload = list(log.iter_raw())[0]["payload"]
    assert payload["storage_state"] is None


def test_recorder_conforms_to_protocol(tmp_path):
    rec, _ = _recorder(tmp_path)
    assert isinstance(rec, BrowserStateStore)


def test_record_is_best_effort_on_log_failure(tmp_path):
    rec, log = _recorder(tmp_path)

    def boom(_event):
        raise OSError("disk full")

    log.append = boom  # type: ignore[assignment]
    # Must swallow the error (never raise into the tool).
    rec.record(["https://a.com"])


def test_replay_collects_latest_browser_state(tmp_path):
    """Replay keeps the most recent browser state (last-write-wins)."""
    rec, log = _recorder(tmp_path)
    rec.record(["https://old.com"], active=0)
    rec.record(
        ["https://new.com", "https://x.com"],
        active=1,
        storage_state={"cookies": [], "origins": []},
    )

    result = replay(log)
    assert result.browser_state is not None
    assert result.browser_state["urls"] == ["https://new.com", "https://x.com"]
    assert result.browser_state["active"] == 1
    assert result.browser_state["storage_state"] == {"cookies": [], "origins": []}


def test_replay_no_browser_state_is_none(tmp_path):
    log = SessionLog("empty_browser_sess", base_dir=str(tmp_path))
    log.create(_meta())
    result = replay(log)
    assert result.browser_state is None


def _meta():
    from mote.session.events import SessionMetaEvent

    return SessionMetaEvent(session_id="empty_browser_sess")
