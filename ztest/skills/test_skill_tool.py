#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.product.toolsets.builtin.skill_tool.Skill (the bridge tool).

The Skill tool resolves the live SkillPool (capability ``get_skill_pool``),
renders an inline skill's body as the tool result, delegates ``context: fork``
skills to ``run_skill_fork``, and searches the long tail via ``query``. Tests
inject the needed capabilities directly (mirroring ``BaseTool.bind`` setattr)
rather than standing up a full Role.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.tool.errors import ToolError
from mote.product.skills.skill_pool import SkillPool
from mote.product.toolsets.builtin.skill_tool import Skill

from .conftest import write_skill


def _make_tool(pool, *, session_id="sess-123", cwd="/work", fork=None):
    """Build a Skill tool with capabilities wired in (as bind() would)."""
    tool = Skill()
    tool._session_id = session_id
    tool.get_skill_pool = lambda: pool
    tool.get_cwd = lambda: cwd
    tool.run_skill_fork = fork or _unused_fork
    return tool


async def _unused_fork(**kwargs):  # pragma: no cover - asserts fork not called
    raise AssertionError("run_skill_fork should not be called for inline skills")


def _pool(builtin_dir, names):
    pool = SkillPool(builtin_dir=builtin_dir)
    pool.load_by_names(names)
    return pool


class TestNoSkills:
    def test_empty_pool_raises(self, builtin_dir):
        tool = _make_tool(_pool(builtin_dir, []))
        with pytest.raises(ToolError):
            asyncio.run(tool.call(name="anything"))

    def test_none_pool_raises(self):
        tool = _make_tool(None)
        with pytest.raises(ToolError):
            asyncio.run(tool.call(name="anything"))


class TestArgumentValidation:
    def test_missing_name_and_query_raises(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        with pytest.raises(ToolError):
            asyncio.run(tool.call())

    def test_unknown_name_raises_with_available(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        with pytest.raises(ToolError) as exc:
            asyncio.run(tool.call(name="ghost"))
        assert "alpha" in str(exc.value)

    def test_human_only_skill_raises(self, builtin_dir):
        write_skill(builtin_dir, "manual", extra_meta={"disable_model_invocation": True})
        tool = _make_tool(_pool(builtin_dir, ["manual"]))
        with pytest.raises(ToolError):
            asyncio.run(tool.call(name="manual"))


class TestInline:
    def test_returns_body_as_result(self, builtin_dir):
        write_skill(builtin_dir, "alpha", instructions="DO THE THING")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        result = asyncio.run(tool.call(name="alpha"))
        assert isinstance(result, str)
        assert "DO THE THING" in result

    def test_substitutes_arguments(self, builtin_dir):
        write_skill(builtin_dir, "alpha", instructions="Input was: $ARGUMENTS")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        result = asyncio.run(tool.call(name="alpha", arguments="hello"))
        assert "Input was: hello" in result

    def test_substitutes_session_id(self, builtin_dir):
        write_skill(builtin_dir, "alpha", instructions="Session ${SESSION_ID}")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]), session_id="sid-42")
        result = asyncio.run(tool.call(name="alpha"))
        assert "Session sid-42" in result

    def test_substitutes_skill_dir(self, builtin_dir):
        write_skill(builtin_dir, "alpha", instructions="Dir: ${SKILL_DIR}")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        result = asyncio.run(tool.call(name="alpha"))
        assert str((builtin_dir / "alpha")) in result

    def test_dollar_in_body_not_mangled(self, builtin_dir):
        write_skill(builtin_dir, "alpha", instructions="cost is $5 and $UNKNOWN")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        result = asyncio.run(tool.call(name="alpha"))
        assert "$5" in result
        assert "$UNKNOWN" in result

    def test_inline_registers_rendered_body_as_resource(self, builtin_dir):
        # An inline invocation registers its rendered body (post-substitution)
        # under the skill name so it survives history compaction.
        write_skill(builtin_dir, "alpha", instructions="BODY for $ARGUMENTS")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        captured = []
        tool.register_resource = lambda **kw: captured.append(kw)
        asyncio.run(tool.call(name="alpha", arguments="X"))
        assert captured == [{"id": "alpha", "kind": "skill", "content": "BODY for X"}]

    def test_inline_registration_is_best_effort(self, builtin_dir):
        # A throwing register_resource must not break the tool result.
        write_skill(builtin_dir, "alpha", instructions="BODY")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))

        def boom(**kw):
            raise RuntimeError("registry down")

        tool.register_resource = boom
        result = asyncio.run(tool.call(name="alpha"))
        assert "BODY" in result

    def test_inline_registration_noop_when_unbound(self, builtin_dir):
        # No register_resource capability injected -> the class default no-op
        # stub handles it (silent, still returns the rendered body).
        write_skill(builtin_dir, "alpha", instructions="BODY")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        result = asyncio.run(tool.call(name="alpha"))
        assert "BODY" in result


class TestFork:
    def test_fork_delegates_to_run_skill_fork(self, builtin_dir):
        write_skill(
            builtin_dir,
            "runner",
            instructions="FORK BODY $ARGUMENTS",
            extra_meta={"context": "fork", "allowed-tools": ["Read"], "model": "m1"},
        )
        captured = {}

        async def fake_fork(**kwargs):
            captured.update(kwargs)
            return "child summary"

        tool = _make_tool(_pool(builtin_dir, ["runner"]), fork=fake_fork)
        result = asyncio.run(tool.call(name="runner", arguments="payload"))
        assert result == "child summary"
        assert "FORK BODY payload" in captured["instructions"]
        assert captured["arguments"] == "payload"
        assert captured["allowed_tools"] == ["Read"]
        assert captured["model"] == "m1"

    def test_fork_does_not_register_resource(self, builtin_dir):
        # Fork skills run in an isolated child; their body never enters the main
        # history, so there is nothing to preserve — no resource registration.
        write_skill(builtin_dir, "runner", extra_meta={"context": "fork"})

        async def fake_fork(**kwargs):
            return "child summary"

        tool = _make_tool(_pool(builtin_dir, ["runner"]), fork=fake_fork)
        captured = []
        tool.register_resource = lambda **kw: captured.append(kw)
        asyncio.run(tool.call(name="runner"))
        assert captured == []

    def test_fork_empty_summary_has_fallback(self, builtin_dir):
        write_skill(builtin_dir, "runner", extra_meta={"context": "fork"})

        async def fake_fork(**kwargs):
            return ""

        tool = _make_tool(_pool(builtin_dir, ["runner"]), fork=fake_fork)
        result = asyncio.run(tool.call(name="runner"))
        assert result  # non-empty fallback message


class TestSearch:
    def test_query_matches_name(self, builtin_dir):
        write_skill(builtin_dir, "pdf-maker", description="Build PDFs")
        write_skill(builtin_dir, "other", description="Unrelated")
        tool = _make_tool(_pool(builtin_dir, ["pdf-maker", "other"]))
        result = asyncio.run(tool.call(query="pdf"))
        assert "pdf-maker" in result
        assert "other" not in result

    def test_query_matches_description_case_insensitive(self, builtin_dir):
        write_skill(builtin_dir, "alpha", description="Generate INVOICES quickly")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        result = asyncio.run(tool.call(query="invoice"))
        assert "alpha" in result

    def test_query_no_match(self, builtin_dir):
        write_skill(builtin_dir, "alpha", description="Something")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        result = asyncio.run(tool.call(query="zzzznomatch"))
        assert "No skills match" in result

    def test_query_excludes_human_only(self, builtin_dir):
        write_skill(
            builtin_dir,
            "secret",
            description="secret thing",
            extra_meta={"disable_model_invocation": True},
        )
        tool = _make_tool(_pool(builtin_dir, ["secret"]))
        result = asyncio.run(tool.call(query="secret"))
        assert "No skills match" in result

    def test_name_takes_precedence_over_query(self, builtin_dir):
        write_skill(builtin_dir, "alpha", instructions="ALPHA BODY")
        tool = _make_tool(_pool(builtin_dir, ["alpha"]))
        # Both name and query given → name wins (invokes, not searches).
        result = asyncio.run(tool.call(name="alpha", query="anything"))
        assert "ALPHA BODY" in result
