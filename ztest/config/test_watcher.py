#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.common.config.watcher`` — mtime-poll hot reload."""
from __future__ import annotations

import os

from mote.common.config.loader import load_config
from mote.common.config.watcher import ConfigWatcher


def _write_workdir_cfg(tmp_path, text: str):
    cfg_dir = tmp_path / ".mote"
    cfg_dir.mkdir(exist_ok=True)
    path = cfg_dir / "config.yaml"
    path.write_text(text)
    return path


def test_poll_once_no_change_returns_none(tmp_path):
    watcher = ConfigWatcher(cwd=tmp_path)
    assert watcher.poll_once() is None


def test_poll_once_detects_new_file(tmp_path):
    watcher = ConfigWatcher(cwd=tmp_path)  # no workdir file yet at init
    _write_workdir_cfg(tmp_path, "proxy: added-later\n")
    cfg = watcher.poll_once()
    assert cfg is not None
    assert cfg.proxy == "added-later"
    # subsequent poll with no further change is a no-op
    assert watcher.poll_once() is None


def test_poll_once_detects_content_change_and_fires_callback(tmp_path):
    path = _write_workdir_cfg(tmp_path, "proxy: first\n")
    seen = {}
    watcher = ConfigWatcher(cwd=tmp_path, on_reload=lambda c: seen.update(proxy=c.proxy))

    path.write_text("proxy: second\n")
    # force a distinct mtime so the change is observable regardless of FS resolution
    future = os.stat(path).st_mtime + 10
    os.utime(path, (future, future))

    cfg = watcher.poll_once()
    assert cfg is not None
    assert cfg.proxy == "second"
    assert seen["proxy"] == "second"


def test_poll_once_detects_file_deletion(tmp_path):
    # Baseline proxy from the ambient config stack with NO workdir override — this
    # is whatever the higher layers (project config.yaml / env) resolve to, which
    # is not necessarily empty.
    baseline_proxy = load_config(tmp_path, reload=True).proxy

    path = _write_workdir_cfg(tmp_path, "proxy: workdir-override\n")
    watcher = ConfigWatcher(cwd=tmp_path)  # snapshot now includes the workdir file
    path.unlink()
    cfg = watcher.poll_once()
    assert cfg is not None
    # With the workdir override gone, proxy falls back to the ambient baseline
    # (not the override we deleted).
    assert cfg.proxy == baseline_proxy
    assert cfg.proxy != "workdir-override"


def test_start_stop_is_idempotent(tmp_path):
    watcher = ConfigWatcher(cwd=tmp_path, interval=0.01)
    watcher.start()
    watcher.start()  # no-op while alive
    watcher.stop(timeout=2.0)
    # safe to stop again
    watcher.stop(timeout=2.0)
