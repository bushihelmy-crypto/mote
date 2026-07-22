#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the markdown-defined Role loader.

A project (or the user) declares a spawnable subagent by dropping a Markdown file
with YAML frontmatter under ``.mote/agents/``. These tests pin the loader's
contract: frontmatter (name/description/tools/model/aliases) + body is parsed
into a ``(BaseAgent, Role)`` subclass carrying class-level ``tools`` (read by the
Agent tool without instantiating), registration is
idempotent, a rescan cleanly *replaces* a prior markdown agent (aliases included),
and a hand-written Python agent always wins over a same-named markdown file.

Discovery is redirected at a tmp tree by monkeypatching the discovery helpers the
loader funnels through, so nothing touches the real ``.mote/agents`` on disk.
"""
import pytest

import mote.roles.agents.markdown_loader as loader
from mote.roles.agents.markdown_loader import _normalize_tools, discover_md_agents, register_md_agents


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


class TestRegister:
    @pytest.fixture(autouse=True)
    def _restore_registry(self):
        # These tests seed the GLOBAL agent-registry singleton (register_md_agents
        # + a hand-seeded _PyAgent). Snapshot/restore around each so the mutation
        # never leaks into sibling suites — e.g. the Agent tool's custom_schema()
        # iterates all_agents() and would trip over a leaked bare BaseRole.
        from mote.executor.agent_registry import registry

        saved = dict(registry._registry)  # noqa: SLF001 — test-scoped save/restore
        try:
            yield
        finally:
            registry._registry = saved  # noqa: SLF001

    def _registry(self):
        from mote.executor.agent_registry import registry

        return registry

    def test_registers_primary_and_aliases(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(
            agents,
            "reviewer",
            "---\nname: reviewer\ndescription: d\naliases: [rev]\n---\nbody\n",
        )
        _point_discovery_at(monkeypatch, agents)

        names = register_md_agents(tmp_path)
        assert names == ["reviewer"]
        reg = self._registry()
        cls = reg.get("reviewer")
        assert cls is not None
        assert reg.get("rev") is cls

    def test_rescan_replaces_and_keeps_aliases_consistent(self, tmp_path, monkeypatch):
        agents = tmp_path / ".mote" / "agents"
        _write_agent(
            agents,
            "reviewer",
            "---\nname: reviewer\ndescription: d\naliases: [rev]\n---\nbody\n",
        )
        _point_discovery_at(monkeypatch, agents)

        register_md_agents(tmp_path)
        register_md_agents(tmp_path)  # rescan builds a fresh class
        reg = self._registry()
        prim = reg.get("reviewer")
        # Alias must not dangle at the stale pre-rescan class.
        assert reg.get("rev") is prim

    def test_python_agent_wins_over_markdown(self, tmp_path, monkeypatch):
        from mote.common.base.role import BaseRole

        reg = self._registry()

        # A hand-written (non-markdown) agent squats the name first.
        class _PyAgent(BaseRole):
            agent_name = "pyfixed"

        reg._registry["pyfixed"] = _PyAgent  # noqa: SLF001 — direct seed for the test

        agents = tmp_path / ".mote" / "agents"
        _write_agent(agents, "pyfixed", "---\nname: pyfixed\ndescription: d\n---\nbody\n")
        _point_discovery_at(monkeypatch, agents)

        names = register_md_agents(tmp_path)
        assert "pyfixed" not in names  # skipped — Python agent kept
        assert reg.get("pyfixed") is _PyAgent
