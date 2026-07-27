#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the global hooks loader (``common.hook.config_source``).

Pins the file-loading seam: ``~/.mote/hooks.json`` (+ project ``.mote/hooks.json``)
are read, layered (concatenated per event), and validated into a ``HookConfig``.
Everything is best-effort — missing / empty / malformed → ``None``, never raises.

Isolation: ``monkeypatch.setattr(paths, "CONFIG_ROOT", tmp)`` retargets the user
layer at a temp dir so the developer's real ``~/.mote`` is never touched.
"""
from __future__ import annotations

import json

import mote.runtime.paths as paths
from mote.contracts.settings.hooks import HookConfig
from mote.runtime.hook.config_source import load_global_hooks, merge_hook_configs


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _pretooluse(matcher, command):
    return {"matcher": matcher, "handlers": [{"type": "command", "command": command}]}


class TestLoadGlobalHooks:
    def test_user_only_load(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(paths, "CONFIG_ROOT", home)
        _write(home / "hooks.json", {"hooks": {"PreToolUse": [_pretooluse("Bash", "check.sh")]}})

        cfg = load_global_hooks(cwd=tmp_path / "proj")
        assert cfg is not None
        groups = cfg.events["PreToolUse"]
        assert len(groups) == 1
        assert groups[0].matcher == "Bash"
        assert groups[0].handlers[0].command == "check.sh"

    def test_user_and_project_concat_layering(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(paths, "CONFIG_ROOT", home)
        _write(home / "hooks.json", {"hooks": {"PreToolUse": [_pretooluse("Bash", "user.sh")]}})

        # A project .mote/hooks.json inside a git repo so the walk includes it.
        proj = tmp_path / "proj"
        (proj / ".git").mkdir(parents=True)
        _write(proj / ".mote" / "hooks.json", {"hooks": {"PreToolUse": [_pretooluse("Read", "proj.sh")]}})

        cfg = load_global_hooks(cwd=proj)
        assert cfg is not None
        groups = cfg.events["PreToolUse"]
        # Concatenated across layers (user first, project appended).
        commands = [g.handlers[0].command for g in groups]
        assert commands == ["user.sh", "proj.sh"]

    def test_empty_map_is_none(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(paths, "CONFIG_ROOT", home)
        _write(home / "hooks.json", {"hooks": {}})
        assert load_global_hooks(cwd=tmp_path / "proj") is None

    def test_missing_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "CONFIG_ROOT", tmp_path / "home")
        assert load_global_hooks(cwd=tmp_path / "proj") is None

    def test_malformed_json_is_none(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(paths, "CONFIG_ROOT", home)
        home.mkdir(parents=True)
        (home / "hooks.json").write_text("{not valid json", encoding="utf-8")
        # Malformed file is swallowed (section → {}), so nothing configured.
        assert load_global_hooks(cwd=tmp_path / "proj") is None

    def test_invalid_shape_is_none(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(paths, "CONFIG_ROOT", home)
        # handlers must be a list of command objects; a bogus shape must not raise.
        _write(home / "hooks.json", {"hooks": {"PreToolUse": [{"matcher": 123, "handlers": "nope"}]}})
        assert load_global_hooks(cwd=tmp_path / "proj") is None


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
    from mote.contracts.settings.hooks import HookCommandHandler, HookMatcherGroup

    return HookMatcherGroup(matcher=matcher, handlers=[HookCommandHandler(command=command)])
