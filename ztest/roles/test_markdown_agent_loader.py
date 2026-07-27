#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the markdown-defined Role loader.

A project (or the user) declares a spawnable subagent by dropping a Markdown file
with YAML frontmatter under ``.mote/agents/``. These tests pin the loader's
contract: frontmatter (name/description/tools/model/aliases) + body is parsed
into a ``(BaseAgent, Role)`` subclass carrying class-level ``tools`` (read by the
Agent tool without instantiating), and each scan can be frozen into an isolated,
content-versioned Application catalog.

Discovery is redirected at a tmp tree by monkeypatching the discovery helpers the
loader funnels through, so nothing touches the real ``.mote/agents`` on disk.
"""
import mote.runtime.agent.agents.markdown_loader as loader
from mote.runtime.agent.agents.markdown_loader import _normalize_tools, discover_md_agents
from mote.runtime.tools.agent_registry import AgentCatalog


def _write_agent(dir_path, stem, body):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{stem}.md").write_text(body, encoding="utf-8")


def _point_discovery_at(monkeypatch, agents_dir):
    """Make ``discover_md_agents`` scan only *agents_dir* (user dir empty)."""
    monkeypatch.setattr(loader, "user_mote_dir", lambda subdir: agents_dir.parent / "nonexistent")
    monkeypatch.setattr(loader, "mote_project_dirs", lambda subdir, cwd=None: [agents_dir])


class TestNormalizeTools:
    def test_none_means_all(self):
        assert _normalize_tools(None) is None

    def test_star_means_all(self):
        assert _normalize_tools("*") is None
        assert _normalize_tools("  ") is None

    def test_csv_string(self):
        assert _normalize_tools("Read, Grep , Glob") == ["Read", "Grep", "Glob"]

    def test_list(self):
        assert _normalize_tools(["Read", " Write "]) == ["Read", "Write"]

    def test_empty_list_is_none(self):
        assert _normalize_tools([]) is None


class TestDiscover:
    def test_parses_frontmatter_into_class(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(
            agents,
            "reviewer",
            "---\n"
            "name: reviewer\n"
            "description: Reviews a diff.\n"
            "tools: [Read, Grep, Glob]\n"
            "aliases: [rev, code-reviewer]\n"
            "---\n"
            "You are a careful code reviewer.\n",
        )
        _point_discovery_at(monkeypatch, agents)

        found = discover_md_agents(tmp_path)
        assert list(found) == ["reviewer"]
        cls = found["reviewer"]
        assert cls.agent_name == "reviewer"
        assert cls.tools == ["Read", "Grep", "Glob"]
        assert cls.aliases == ["rev", "code-reviewer"]
        assert cls.description == "Reviews a diff."
        assert cls.get_schema()["description"] == "Reviews a diff."

    def test_missing_description_skipped(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(agents, "bad", "---\nname: bad\n---\nno description here\n")
        _point_discovery_at(monkeypatch, agents)
        assert discover_md_agents(tmp_path) == {}

    def test_name_defaults_to_filename_stem(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(agents, "helper", "---\ndescription: A helper.\n---\nbody\n")
        _point_discovery_at(monkeypatch, agents)
        found = discover_md_agents(tmp_path)
        assert list(found) == ["helper"]

    def test_absent_tools_means_all(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(agents, "a", "---\nname: a\ndescription: d\n---\nbody\n")
        _point_discovery_at(monkeypatch, agents)
        cls = discover_md_agents(tmp_path)["a"]
        # No allowlist declared → class exposes an empty tools list (inherits all
        # via the schema path, but the *listing* attr is empty).
        assert cls.tools == []


class TestCatalog:
    def test_snapshot_resolves_primary_and_aliases(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(
            agents,
            "reviewer",
            "---\nname: reviewer\ndescription: d\naliases: [rev]\n---\nbody\n",
        )
        _point_discovery_at(monkeypatch, agents)

        catalog = AgentCatalog.from_types(discover_md_agents(tmp_path).values())
        cls = catalog.get("reviewer")
        assert cls is not None
        assert catalog.get("rev") is cls

    def test_rescan_produces_new_version_without_mutating_prior_snapshot(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(
            agents,
            "reviewer",
            "---\nname: reviewer\ndescription: d\naliases: [rev]\n---\nbody\n",
        )
        _point_discovery_at(monkeypatch, agents)

        first = AgentCatalog.from_types(discover_md_agents(tmp_path).values())
        _write_agent(
            agents,
            "reviewer",
            "---\nname: reviewer\ndescription: changed\naliases: [review]\n---\nnew body\n",
        )
        second = AgentCatalog.from_types(discover_md_agents(tmp_path).values())
        assert first.version != second.version
        assert first.get("rev") is not None
        assert first.get("review") is None
        assert second.get("rev") is None
        assert second.get("review") is not None
