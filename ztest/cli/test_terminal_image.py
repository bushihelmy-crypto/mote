#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the pluggable native terminal image protocol layer.

The layer is the closed extension point for pixel-perfect inline images: each
:class:`TerminalImageProtocol` owns detection (a cheap env sniff) and encoding
(image → escape sequence), and :func:`detect_image_protocol` picks the first
available one from the registry. These tests pin the Kitty implementation's
detection matrix + APC ``_G`` wire format and the registry's cache/fallback.
"""

from __future__ import annotations

import pytest
from mote.cli.consumers.render.terminal_image import KittyImageProtocol, detect_image_protocol


def _clear_image_env(monkeypatch):
    """Strip every terminal-identifying var so detection starts from a blank slate."""
    for var in (
        "KITTY_WINDOW_ID",
        "TERM",
        "TERM_PROGRAM",
        "GHOSTTY_RESOURCES_DIR",
        "WEZTERM_EXECUTABLE",
    ):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# KittyImageProtocol.detect — the environment sniff matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {"KITTY_WINDOW_ID": "1"},
        {"TERM": "xterm-kitty"},
        {"TERM_PROGRAM": "ghostty"},
        {"TERM_PROGRAM": "WezTerm"},
        {"GHOSTTY_RESOURCES_DIR": "/x"},
        {"WEZTERM_EXECUTABLE": "/usr/bin/wezterm"},
    ],
)
def test_kitty_detects_capable_terminals(monkeypatch, env):
    _clear_image_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert KittyImageProtocol().detect() is True


def test_kitty_rejects_plain_terminal(monkeypatch):
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert KittyImageProtocol().detect() is False


def test_probe_defers_by_default():
    # No dynamic handshake today → probe() returns None (defer to detect()).
    assert KittyImageProtocol().probe() is None


# --------------------------------------------------------------------------
# KittyImageProtocol.encode — the APC _G PNG-base64 wire format
# --------------------------------------------------------------------------


def _png(tmp_path, size=(4, 4), color=(1, 2, 3)):
    pytest.importorskip("PIL")
    from PIL import Image

    path = tmp_path / "pic.png"
    Image.new("RGB", size, color).save(str(path))
    return str(path)


def test_encode_emits_apc_graphics_header(tmp_path):
    seq = KittyImageProtocol().encode(_png(tmp_path), max_cols=40, max_rows=20)
    assert seq is not None
    assert seq.startswith("\x1b_G")  # APC graphics introducer
    assert seq.endswith("\x1b\\")  # APC terminator (ST)
    # First chunk carries the image control keys: PNG format, transmit+display,
    # and the cell box we asked it to fit into.
    assert "f=100" in seq and "a=T" in seq
    assert "c=40" in seq and "r=20" in seq


def _noise_png(tmp_path, size=(200, 200)):
    # Random pixels defeat PNG compression, so the base64 payload reliably
    # spills past a single 4096-byte chunk (a flat colour would compress away).
    pytest.importorskip("PIL")
    import os

    from PIL import Image

    path = tmp_path / "noise.png"
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(str(path))
    return str(path)


def test_encode_chunks_large_payload(tmp_path):
    # A bigger image spills past one 4096-byte base64 chunk → multiple APC frames,
    # every one but the last flagged ``m=1`` (more follows), the last ``m=0``.
    seq = KittyImageProtocol().encode(_noise_png(tmp_path))
    assert seq is not None
    assert seq.count("\x1b_G") > 1  # more than one frame
    assert "m=1" in seq  # continuation frames
    assert "m=0" in seq  # final frame


def test_encode_missing_file_returns_none():
    assert KittyImageProtocol().encode("/no/such/image.png") is None


# --------------------------------------------------------------------------
# detect_image_protocol — registry lookup + cache
# --------------------------------------------------------------------------


def test_registry_returns_kitty_when_available(monkeypatch):
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("TERM", "xterm-kitty")
    proto = detect_image_protocol(force=True)
    assert proto is not None
    assert proto.name == "kitty"


def test_registry_returns_none_without_protocol(monkeypatch):
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("TERM", "dumb")
    assert detect_image_protocol(force=True) is None


def test_registry_caches_until_forced(monkeypatch):
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("TERM", "xterm-kitty")
    first = detect_image_protocol(force=True)
    assert first is not None
    # Env now says a plain terminal, but the cached result stands until forced.
    monkeypatch.setenv("TERM", "xterm-256color")
    assert detect_image_protocol() is first
    assert detect_image_protocol(force=True) is None
