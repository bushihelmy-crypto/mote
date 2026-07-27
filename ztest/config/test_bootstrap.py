#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the ``~/.mote`` first-run scaffolding (``common.config.bootstrap``).

Pin the two invariants that make it safe to run on every CLI launch: it seeds
the config-home templates when absent, and it is strictly non-destructive
(never clobbers an existing file) and best-effort (never raises).
"""
from __future__ import annotations

import json

import mote.runtime.config.bootstrap as bootstrap
from mote.runtime.config.bootstrap import ensure_mote_home


class TestFreshHome:
    def test_seeds_all_templates(self, tmp_path):
        base = tmp_path / ".mote"
        ensure_mote_home(base)

        # config.yaml is the packaged annotated example (has the models section).
        config = base / "config.yaml"
        assert config.is_file()
        assert "models:" in config.read_text(encoding="utf-8")

        # mcp.json is a valid empty {"mcpServers": {}} map.
        mcp = base / "mcp.json"
        assert json.loads(mcp.read_text(encoding="utf-8")) == {"mcpServers": {}}

        # hooks.json is a valid empty {"hooks": {}} map.
        hooks = base / "hooks.json"
        assert json.loads(hooks.read_text(encoding="utf-8")) == {"hooks": {}}

        # secrets_config.json is a valid empty flat map.
        secrets = base / "secrets_config.json"
        assert json.loads(secrets.read_text(encoding="utf-8")) == {}

        # skills/ directory exists.
        assert (base / "skills").is_dir()

    def test_defaults_to_config_root(self, tmp_path, monkeypatch):
        # No explicit root => uses the module's CONFIG_ROOT (monkeypatched here
        # so we never touch the developer's real ~/.mote).
        home = tmp_path / "home_mote"
        monkeypatch.setattr(bootstrap.paths, "CONFIG_ROOT", home)
        ensure_mote_home()
        assert (home / "config.yaml").is_file()
        assert (home / "mcp.json").is_file()


class TestNonDestructive:
    def test_does_not_overwrite_existing(self, tmp_path):
        base = tmp_path / ".mote"
        base.mkdir()
        (base / "config.yaml").write_text("user: edited", encoding="utf-8")
        (base / "mcp.json").write_text('{"mcpServers": {"x": {"command": "foo"}}}', encoding="utf-8")
        (base / "hooks.json").write_text('{"hooks": {"Stop": []}}', encoding="utf-8")

        ensure_mote_home(base)

        # Existing files are left byte-for-byte untouched.
        assert (base / "config.yaml").read_text(encoding="utf-8") == "user: edited"
        assert json.loads((base / "mcp.json").read_text(encoding="utf-8")) == {"mcpServers": {"x": {"command": "foo"}}}
        assert json.loads((base / "hooks.json").read_text(encoding="utf-8")) == {"hooks": {"Stop": []}}
        # Missing ones are still seeded.
        assert (base / "secrets_config.json").is_file()
        assert (base / "skills").is_dir()

    def test_idempotent(self, tmp_path):
        base = tmp_path / ".mote"
        ensure_mote_home(base)
        ensure_mote_home(base)  # second run must be a no-op, not an error.
        assert (base / "config.yaml").is_file()


class TestBestEffort:
    def test_unwritable_home_does_not_raise(self, tmp_path, monkeypatch):
        base = tmp_path / ".mote"

        def _boom(*_a, **_k):
            raise OSError("read-only home")

        monkeypatch.setattr(bootstrap.Path, "mkdir", _boom)
        # Must swallow the OSError and return cleanly (startup must not break).
        ensure_mote_home(base)
