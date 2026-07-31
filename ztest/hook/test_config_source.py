#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the global hooks loader (``common.hook.config_source``).

Pins the file-loading seam: ``~/.mote/hooks.json`` (+ project ``.mote/hooks.json``)
are read, layered (concatenated per event), and validated into a ``HookConfig``.
Everything is best-effort — missing / empty / malformed → ``None``, never raises.

Tests inject a Product-owned source provider rooted in a temporary directory.
"""
from __future__ import annotations

import json

from mote.product.config.adapters.hooks import load_global_hooks, merge_hook_configs
from mote.product.paths import default_runtime_paths, mote_layered_files
from mote.runtime.config.hook import HookConfig


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _provider(home, cwd):
    paths = default_runtime_paths(user_config_root=home)
    return mote_layered_files("hooks.json", cwd, user_config_root=paths.user_config_root)


def _pretooluse(matcher, command):
    return {"matcher": matcher, "handlers": [{"type": "command", "command": command}]}


class TestLoadGlobalHooks:
    def test_user_only_load(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        _write(
            home / "hooks.json",
            {"hooks": {"PreToolUse": [_pretooluse("Bash", "check.sh")]}},
        )

        cfg = load_global_hooks(_provider(home, tmp_path / "proj"))
        assert cfg is not None
        groups = cfg.events["PreToolUse"]
        assert len(groups) == 1
        assert groups[0].matcher == "Bash"
        assert groups[0].handlers[0].command == "check.sh"

    def test_user_and_project_concat_layering(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        _write(
            home / "hooks.json",
            {"hooks": {"PreToolUse": [_pretooluse("Bash", "user.sh")]}},
        )

        # A project .mote/hooks.json inside a git repo so the walk includes it.
        proj = tmp_path / "proj"
        (proj / ".git").mkdir(parents=True)
        _write(
            proj / ".mote" / "hooks.json",
            {"hooks": {"PreToolUse": [_pretooluse("Read", "proj.sh")]}},
        )

        cfg = load_global_hooks(_provider(home, proj))
        assert cfg is not None
        groups = cfg.events["PreToolUse"]
        # Concatenated across layers (user first, project appended).
        commands = [g.handlers[0].command for g in groups]
        assert commands == ["user.sh", "proj.sh"]

    def test_empty_map_is_none(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        _write(home / "hooks.json", {"hooks": {}})
        assert load_global_hooks(_provider(home, tmp_path / "proj")) is None

    def test_missing_is_none(self, tmp_path, monkeypatch):
        assert load_global_hooks(_provider(tmp_path / "home", tmp_path / "proj")) is None

    def test_malformed_json_is_none(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(parents=True)
        (home / "hooks.json").write_text("{not valid json", encoding="utf-8")
        # Malformed file is swallowed (section → {}), so nothing configured.
        assert load_global_hooks(_provider(home, tmp_path / "proj")) is None

    def test_invalid_shape_is_none(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        # handlers must be a list of command objects; a bogus shape must not raise.
        _write(
            home / "hooks.json",
            {"hooks": {"PreToolUse": [{"matcher": 123, "handlers": "nope"}]}},
        )
        assert load_global_hooks(_provider(home, tmp_path / "proj")) is None


class TestMergeHookConfigs:
    def test_concats_per_event(self):
        a = HookConfig(events={"PreToolUse": [_hmg("Bash", "a.sh")]})
        b = HookConfig(events={"PreToolUse": [_hmg("Read", "b.sh")], "Stop": [_hmg(None, "s.sh")]})
        merged = merge_hook_configs(a, b)
        assert merged is not None
        commands = [g.handlers[0].command for g in merged.events["PreToolUse"]]
        assert commands == ["a.sh", "b.sh"]
        assert merged.events["Stop"][0].handlers[0].command == "s.sh"

    def test_none_and_empty_yield_none(self):
        assert merge_hook_configs(None, None) is None
        assert merge_hook_configs() is None

    def test_skips_none_configs(self):
        a = HookConfig(events={"PreToolUse": [_hmg("Bash", "a.sh")]})
        merged = merge_hook_configs(None, a, None)
        assert merged is not None
        assert merged.events["PreToolUse"][0].handlers[0].command == "a.sh"


def _hmg(matcher, command):
    from mote.runtime.config.hook import HookCommandHandler, HookMatcherGroup

    return HookMatcherGroup(matcher=matcher, handlers=[HookCommandHandler(command=command)])
