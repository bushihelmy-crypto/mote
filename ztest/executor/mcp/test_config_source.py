#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the MCP config source (``executor.mcp.config_source``).

MCP servers are defined in their own Claude-style ``mcp_config.json`` file
(``{"mcpServers": {name: {...}}}``), NOT the layered ``config.yaml``. These
tests pin the loader's contract: the map shape parses, transport is *inferred*
(``url`` => SSE, ``command`` => STDIO), presence means enabled, and every bad
input is best-effort (missing / empty / malformed / bad-entry => empty list or
dropped entry, never an exception).

All tests point the loader at a tmp file by monkeypatching ``mcp_config_paths``
(the single seam every public function funnels through), so nothing touches the
real ``.mote/mcp.json`` files on disk.
"""
import json

import pytest

from mote.common.config.config.mcp_config import MCPTransportType
from mote.common.const import paths
from mote.executor.mcp import config_source
from mote.executor.mcp.config_source import MCP_CONFIG_FILE_NAME, load_mcp_servers, mcp_config_paths


@pytest.fixture
def mcp_file(tmp_path, monkeypatch):
    """A tmp ``mcp.json`` wired in as the loader's only config path.

    Returns the ``Path``; write to it (or leave it absent) per-test. The loader
    resolves through ``mcp_config_paths`` (list, low→high) so patching that one
    function to return ``[path]`` (only when it exists) redirects every read here.
    """
    path = tmp_path / MCP_CONFIG_FILE_NAME
    monkeypatch.setattr(config_source, "mcp_config_paths", lambda cwd=None: [path] if path.is_file() else [])
    return path


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


class TestMcpConfigPaths:
    def test_file_name_constant(self):
        assert MCP_CONFIG_FILE_NAME == "mcp.json"

    def test_paths_include_user_file_when_present(self, tmp_path, monkeypatch):
        # ``~/.mote/mcp.json`` (CONFIG_ROOT) is the lowest layer; when it exists
        # it leads the returned list, followed by the project walk. The discovery
        # lives in ``common.const.paths.mote_layered_files``, so patch there.
        user_file = tmp_path / MCP_CONFIG_FILE_NAME
        user_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(paths, "CONFIG_ROOT", tmp_path)
        monkeypatch.setattr(paths, "mote_project_files", lambda name, cwd=None: [])
        assert mcp_config_paths() == [user_file]

    def test_paths_empty_when_nothing_configured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "CONFIG_ROOT", tmp_path)  # no mcp.json inside
        monkeypatch.setattr(paths, "mote_project_files", lambda name, cwd=None: [])
        assert mcp_config_paths() == []


class TestLoadMissingOrEmpty:
    def test_missing_file_returns_empty(self, mcp_file):
        # File never created.
        assert load_mcp_servers() == []

    def test_empty_file_returns_empty(self, mcp_file):
        mcp_file.write_text("", encoding="utf-8")
        assert load_mcp_servers() == []

    def test_whitespace_only_returns_empty(self, mcp_file):
        mcp_file.write_text("   \n  ", encoding="utf-8")
        assert load_mcp_servers() == []

    def test_malformed_json_returns_empty(self, mcp_file):
        mcp_file.write_text("{not valid json", encoding="utf-8")
        assert load_mcp_servers() == []

    def test_no_mcp_servers_key_returns_empty(self, mcp_file):
        _write(mcp_file, {"somethingElse": {}})
        assert load_mcp_servers() == []

    def test_mcp_servers_not_a_dict_returns_empty(self, mcp_file):
        _write(mcp_file, {"mcpServers": ["not", "a", "map"]})
        assert load_mcp_servers() == []

    def test_top_level_not_a_dict_returns_empty(self, mcp_file):
        _write(mcp_file, ["a", "list"])
        assert load_mcp_servers() == []

    def test_empty_servers_map_returns_empty(self, mcp_file):
        _write(mcp_file, {"mcpServers": {}})
        assert load_mcp_servers() == []


class TestTransportInference:
    def test_command_infers_stdio(self, mcp_file):
        _write(mcp_file, {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "server-fs"]}}})
        servers = load_mcp_servers()
        assert len(servers) == 1
        s = servers[0]
        assert s.name == "fs"
        assert s.type == MCPTransportType.STDIO
        assert s.command == "npx"
        assert s.args == ["-y", "server-fs"]
        assert s.url is None
        assert s.enabled is True

    def test_url_infers_sse(self, mcp_file):
        _write(mcp_file, {"mcpServers": {"remote": {"url": "https://example.com/sse"}}})
        servers = load_mcp_servers()
        assert len(servers) == 1
        s = servers[0]
        assert s.name == "remote"
        assert s.type == MCPTransportType.SSE
        assert s.url == "https://example.com/sse"
        assert s.command is None
        assert s.enabled is True

    def test_url_wins_when_both_present(self, mcp_file):
        # A url is checked first, so it takes precedence over a stray command.
        _write(mcp_file, {"mcpServers": {"both": {"url": "https://x/sse", "command": "npx"}}})
        servers = load_mcp_servers()
        assert servers[0].type == MCPTransportType.SSE


class TestBestEffortEntries:
    def test_entry_with_neither_url_nor_command_dropped(self, mcp_file):
        _write(
            mcp_file,
            {
                "mcpServers": {
                    "good": {"command": "npx"},
                    "bad": {"env": {"X": "1"}},  # neither url nor command
                }
            },
        )
        servers = load_mcp_servers()
        assert [s.name for s in servers] == ["good"]

    def test_non_object_entry_dropped(self, mcp_file):
        _write(
            mcp_file,
            {
                "mcpServers": {
                    "good": {"url": "https://x/sse"},
                    "bad": "not-an-object",
                }
            },
        )
        servers = load_mcp_servers()
        assert [s.name for s in servers] == ["good"]

    def test_env_and_aliases_passed_through(self, mcp_file):
        _write(
            mcp_file,
            {
                "mcpServers": {
                    "srv": {
                        "command": "npx",
                        "env": {"TOKEN": "abc"},
                        "aliases": {"search": ["find", "lookup"]},
                    }
                }
            },
        )
        s = load_mcp_servers()[0]
        assert s.env == {"TOKEN": "abc"}
        assert s.aliases == {"search": ["find", "lookup"]}

    def test_missing_args_defaults_to_empty_list(self, mcp_file):
        _write(mcp_file, {"mcpServers": {"srv": {"command": "npx"}}})
        s = load_mcp_servers()[0]
        assert s.args == []

    def test_multiple_servers_preserve_order(self, mcp_file):
        _write(
            mcp_file,
            {
                "mcpServers": {
                    "a": {"command": "a-cmd"},
                    "b": {"url": "https://b/sse"},
                    "c": {"command": "c-cmd"},
                }
            },
        )
        assert [s.name for s in load_mcp_servers()] == ["a", "b", "c"]


class TestLayerMerge:
    """The git-root→cwd walk merges by server name; a closer file overrides."""

    def test_closer_file_overrides_same_name(self, tmp_path, monkeypatch):
        far = tmp_path / "far.json"
        near = tmp_path / "near.json"
        _write(far, {"mcpServers": {"srv": {"command": "far-cmd"}}})
        _write(near, {"mcpServers": {"srv": {"command": "near-cmd"}}})
        # low→high precedence: far first, near last (closer wins).
        monkeypatch.setattr(config_source, "mcp_config_paths", lambda cwd=None: [far, near])
        servers = load_mcp_servers()
        assert len(servers) == 1
        assert servers[0].command == "near-cmd"

    def test_distinct_names_union_across_layers(self, tmp_path, monkeypatch):
        far = tmp_path / "far.json"
        near = tmp_path / "near.json"
        _write(far, {"mcpServers": {"a": {"command": "a-cmd"}}})
        _write(near, {"mcpServers": {"b": {"url": "https://b/sse"}}})
        monkeypatch.setattr(config_source, "mcp_config_paths", lambda cwd=None: [far, near])
        assert sorted(s.name for s in load_mcp_servers()) == ["a", "b"]
