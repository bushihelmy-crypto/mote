#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the settings config source (``executor.permission.settings_source``).

Permission rules can be declared per-project in a Claude-style
``.mote/settings.local.json`` (``{"permissions": {"allow"/"deny"/"ask": [...]}}``),
discovered by walking from cwd up to the git root plus a user-level
``~/.mote/settings.local.json``. These tests pin the loader's contract: the
allow/deny/ask lists are unioned across layers (order preserved, de-duplicated),
every bad input is best-effort (missing / empty / malformed / bad-shape => no
rules, never an exception), and an all-empty result returns ``None`` so a caller
can leave a Role's existing policy untouched.

All tests point the loader at tmp files by monkeypatching ``settings_paths``
(the single seam ``load_permission_rules`` funnels through), so nothing touches
the real ``.mote/settings.local.json`` files on disk.
"""
import json

import pytest

from mote.common.const import paths
from mote.executor.permission import settings_source
from mote.executor.permission.settings_source import SETTINGS_FILE_NAME, load_permission_rules, settings_paths


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """A tmp ``settings.local.json`` wired in as the loader's only settings path.

    Returns the ``Path``; write to it (or leave it absent) per-test. The loader
    resolves through ``settings_paths`` (list, low→high) so patching that one
    function to return ``[path]`` (only when it exists) redirects every read here.
    """
    path = tmp_path / SETTINGS_FILE_NAME
    monkeypatch.setattr(settings_source, "settings_paths", lambda cwd=None: [path] if path.is_file() else [])
    return path


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


class TestSettingsPaths:
    def test_file_name_constant(self):
        assert SETTINGS_FILE_NAME == "settings.local.json"

    def test_paths_include_user_file_when_present(self, tmp_path, monkeypatch):
        # ``~/.mote/settings.local.json`` (CONFIG_ROOT) is the lowest layer; when
        # it exists it leads the returned list, followed by the project walk. The
        # discovery lives in ``common.const.paths.mote_layered_files``, so patch
        # the names there.
        user_file = tmp_path / SETTINGS_FILE_NAME
        user_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(paths, "CONFIG_ROOT", tmp_path)
        monkeypatch.setattr(paths, "mote_project_files", lambda name, cwd=None: [])
        assert settings_paths() == [user_file]

    def test_paths_empty_when_nothing_configured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "CONFIG_ROOT", tmp_path)  # no file inside
        monkeypatch.setattr(paths, "mote_project_files", lambda name, cwd=None: [])
        assert settings_paths() == []


class TestLoadMissingOrEmpty:
    def test_missing_file_returns_none(self, settings_file):
        # File never created.
        assert load_permission_rules() is None

    def test_empty_file_returns_none(self, settings_file):
        settings_file.write_text("", encoding="utf-8")
        assert load_permission_rules() is None

    def test_whitespace_only_returns_none(self, settings_file):
        settings_file.write_text("   \n  ", encoding="utf-8")
        assert load_permission_rules() is None

    def test_malformed_json_returns_none(self, settings_file):
        settings_file.write_text("{not valid json", encoding="utf-8")
        assert load_permission_rules() is None

    def test_no_permissions_key_returns_none(self, settings_file):
        _write(settings_file, {"somethingElse": {}})
        assert load_permission_rules() is None

    def test_permissions_not_a_dict_returns_none(self, settings_file):
        _write(settings_file, {"permissions": ["not", "a", "map"]})
        assert load_permission_rules() is None

    def test_top_level_not_a_dict_returns_none(self, settings_file):
        _write(settings_file, ["a", "list"])
        assert load_permission_rules() is None

    def test_empty_rule_lists_return_none(self, settings_file):
        _write(settings_file, {"permissions": {"allow": [], "deny": [], "ask": []}})
        assert load_permission_rules() is None


class TestRuleParsing:
    def test_single_layer_all_buckets(self, settings_file):
        _write(
            settings_file,
            {
                "permissions": {
                    "allow": ["Read", "Grep", "Bash(git*)"],
                    "deny": ["Bash(rm -rf*)"],
                    "ask": ["Write"],
                }
            },
        )
        cfg = load_permission_rules()
        assert cfg is not None
        assert cfg.allow == ["Read", "Grep", "Bash(git*)"]
        assert cfg.deny == ["Bash(rm -rf*)"]
        assert cfg.ask == ["Write"]

    def test_partial_buckets(self, settings_file):
        _write(settings_file, {"permissions": {"allow": ["Read"]}})
        cfg = load_permission_rules()
        assert cfg is not None
        assert cfg.allow == ["Read"]
        assert cfg.deny == []
        assert cfg.ask == []

    def test_non_string_entries_dropped(self, settings_file):
        _write(
            settings_file,
            {"permissions": {"allow": ["Read", 42, None, {"x": 1}, "Grep"]}},
        )
        cfg = load_permission_rules()
        assert cfg is not None
        assert cfg.allow == ["Read", "Grep"]

    def test_whitespace_entries_stripped_and_blanks_dropped(self, settings_file):
        _write(settings_file, {"permissions": {"allow": ["  Read  ", "   ", ""]}})
        cfg = load_permission_rules()
        assert cfg is not None
        assert cfg.allow == ["Read"]

    def test_non_list_bucket_ignored(self, settings_file):
        _write(settings_file, {"permissions": {"allow": "Read", "deny": ["X"]}})
        cfg = load_permission_rules()
        assert cfg is not None
        assert cfg.allow == []
        assert cfg.deny == ["X"]


class TestLayerUnion:
    """The git-root→cwd walk unions rule lists; order preserved, de-duplicated."""

    def test_lists_union_across_layers(self, tmp_path, monkeypatch):
        far = tmp_path / "far.json"
        near = tmp_path / "near.json"
        _write(far, {"permissions": {"allow": ["Read"], "deny": ["Bash(rm*)"]}})
        _write(near, {"permissions": {"allow": ["Grep"], "ask": ["Write"]}})
        monkeypatch.setattr(settings_source, "settings_paths", lambda cwd=None: [far, near])
        cfg = load_permission_rules()
        assert cfg is not None
        # low→high order: far's rules first, then near's (appended).
        assert cfg.allow == ["Read", "Grep"]
        assert cfg.deny == ["Bash(rm*)"]
        assert cfg.ask == ["Write"]

    def test_duplicate_rules_deduped_across_layers(self, tmp_path, monkeypatch):
        far = tmp_path / "far.json"
        near = tmp_path / "near.json"
        _write(far, {"permissions": {"allow": ["Read", "Grep"]}})
        _write(near, {"permissions": {"allow": ["Grep", "Glob"]}})
        monkeypatch.setattr(settings_source, "settings_paths", lambda cwd=None: [far, near])
        cfg = load_permission_rules()
        assert cfg is not None
        # "Grep" appears in both; kept once, first-seen order.
        assert cfg.allow == ["Read", "Grep", "Glob"]

    def test_closer_layer_cannot_drop_farther_deny(self, tmp_path, monkeypatch):
        # A closer file only ever *adds* rules — it can't silently drop a
        # farther layer's deny (the union guarantee).
        far = tmp_path / "far.json"
        near = tmp_path / "near.json"
        _write(far, {"permissions": {"deny": ["Bash(rm -rf*)"]}})
        _write(near, {"permissions": {"allow": ["Read"]}})
        monkeypatch.setattr(settings_source, "settings_paths", lambda cwd=None: [far, near])
        cfg = load_permission_rules()
        assert cfg is not None
        assert cfg.deny == ["Bash(rm -rf*)"]
        assert cfg.allow == ["Read"]

    def test_one_bad_layer_does_not_break_the_union(self, tmp_path, monkeypatch):
        good = tmp_path / "good.json"
        bad = tmp_path / "bad.json"
        _write(good, {"permissions": {"allow": ["Read"]}})
        bad.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(settings_source, "settings_paths", lambda cwd=None: [good, bad])
        cfg = load_permission_rules()
        assert cfg is not None
        assert cfg.allow == ["Read"]
