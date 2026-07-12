#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.think.prompt_builder — PromptBuilder + ThinkContext.

PromptBuilder is a stateless assembler: every method is a pure function over
ThinkInputs / ThinkContext / the four subsystems. The tests cover the system &
user prompt assembly (placeholder substitution + cache-boundary removal +
memory/reminder injection), each ``_make_*`` section builder, and the full
collect_context() integration through the duck-typed fakes in conftest.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.common import prompt as R
from mote.common.base.command_channel import PROMPT_VAR_KEYS
from mote.think.prompt_builder import PromptBuilder, ThinkContext, ThinkInputs, ThinkSubsystems

from .conftest import FakeExecutor, FakeInjector, FakeSkillManager, make_config


def run(coro):
    return asyncio.run(coro)


class _FakeChannel:
    """Minimal command-channel stand-in: supplies prompt_vars + an identity lower."""

    def __init__(self, prompt_vars: dict):
        self._vars = prompt_vars

    def prompt_vars(self) -> dict:
        return self._vars

    def lower(self, text: str) -> str:
        return text


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------
class TestDataclasses:
    def test_think_inputs_defaults(self):
        ti = ThinkInputs()
        assert ti.working_dir == ""
        assert ti.memory_dir is None

    def test_think_context_defaults(self):
        tc = ThinkContext()
        assert tc.env_section == ""
        # The protocol fills default to empty strings until a channel supplies them.
        assert tc.prompt_vars == {k: "" for k in PROMPT_VAR_KEYS}


# --------------------------------------------------------------------------
# join_sections
# --------------------------------------------------------------------------
class TestJoinSections:
    def test_drops_none_and_blank(self):
        out = PromptBuilder.join_sections(["a", None, "", "   ", "b"])
        assert out == "a\nb"

    def test_all_empty_returns_empty(self):
        assert PromptBuilder.join_sections([None, "", "  "]) == ""


# --------------------------------------------------------------------------
# System / user prompt assembly
# --------------------------------------------------------------------------
class TestBuildSystemPrompt:
    def test_substitutes_and_strips_boundary(self):
        ctx = ThinkContext(env_section="ENVBLOCK")
        sys_p = PromptBuilder._build_system_prompt(R.SYSTEM_PROMPT, ctx)
        assert "ENVBLOCK" in sys_p
        # boundary marker removed
        assert R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in sys_p
        # no unresolved placeholders for the keys we mapped
        assert "${env_section}" not in sys_p

    def test_missing_placeholder_tolerated(self):
        # safe_substitute: a template with an unknown $foo is left intact, no raise.
        ctx = ThinkContext()
        out = PromptBuilder._build_system_prompt("hello $unknown ${env_section}", ctx)
        assert "$unknown" in out


class TestBuildUserPrompt:
    def test_empty_base_yields_empty_when_no_context(self):
        # cwd + timestamp moved off the tail into per-turn reminder sources and the
        # base template is now empty, so with no memory/reminders the tail is empty
        # — no dangling "# Current State" header, no "current directory" line.
        ctx = ThinkContext(working_dir="/work")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out == ""
        assert "current directory" not in out
        assert "Current State" not in out

    def test_prepends_memory_context(self):
        ctx = ThinkContext(working_dir="/w", memory_context="# MEMORY.md\nidx")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out.startswith("# MEMORY.md\nidx")

    def test_appends_reminders(self):
        ctx = ThinkContext(working_dir="/w", reminders="REMIND")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out.rstrip().endswith("REMIND")

    def test_memory_and_reminders_together(self):
        ctx = ThinkContext(working_dir="/w", memory_context="MEM", reminders="REM")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out.startswith("MEM")
        assert out.rstrip().endswith("REM")


class TestBuildTuple:
    def test_build_returns_pair(self):
        ctx = ThinkContext(env_section="ENVBLOCK", working_dir="/w")
        sys_p, usr_p = PromptBuilder.build(R.SYSTEM_PROMPT, R.CMD_PROMPT, ctx)
        assert "ENVBLOCK" in sys_p
        assert isinstance(usr_p, str)


# --------------------------------------------------------------------------
# Substitution maps
# --------------------------------------------------------------------------
class TestSubstitutionMaps:
    def test_system_substitutions_keys(self):
        # The volatile catalog placeholders (available_commands / mcp_tools /
        # pipeline_tools) are gone — the catalog rides the per-turn reminder now.
        # The static protocol sections come from ctx.prompt_vars (command_guide /
        # tool_usage_guide), merged in via **ctx.prompt_vars.
        ctx = ThinkContext(
            env_section="ENVBLOCK",
            prompt_vars={"command_guide": "CG", "tool_usage_guide": "TUG"},
        )
        d = PromptBuilder._system_substitutions(ctx)
        assert d["env_section"] == "ENVBLOCK"
        assert d["command_guide"] == "CG"
        assert d["tool_usage_guide"] == "TUG"

    def test_user_substitutions_keys(self):
        # current_state is now empty: cwd + time moved to per-turn reminder sources.
        ctx = ThinkContext(working_dir="/here")
        d = PromptBuilder._user_substitutions(ctx)
        assert d["current_state"] == ""


# --------------------------------------------------------------------------
# _make_* section builders
# --------------------------------------------------------------------------
class TestMakeMemory:
    def test_no_dir_returns_empty_pair(self):
        assert PromptBuilder._make_memory(None) == ("", "")
        assert PromptBuilder._make_memory("") == ("", "")

    def test_with_dir_reads_index(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("- [A](a.md) — hook", encoding="utf-8")
        instructions, context = PromptBuilder._make_memory(tmp_path)
        assert str(tmp_path) in instructions  # memory_dir substituted
        assert "- [A](a.md) — hook" in context

    def test_missing_index_uses_empty_state(self, tmp_path):
        from mote.common.prompt.memory import MEMORY_EMPTY_STATE

        instructions, context = PromptBuilder._make_memory(tmp_path)
        assert instructions  # still produces instructions
        assert MEMORY_EMPTY_STATE in context


class TestReadMemoryIndex:
    def test_reads_and_strips(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("  content  \n", encoding="utf-8")
        assert PromptBuilder._read_memory_index(tmp_path) == "content"

    def test_absent_returns_empty(self, tmp_path):
        assert PromptBuilder._read_memory_index(tmp_path) == ""


class TestMakeReminders:
    def test_none_bus_returns_empty(self):
        import asyncio

        assert asyncio.run(PromptBuilder._make_reminders(None, "/work")) == ""

    def test_delegates_to_bus_collect(self):
        import asyncio

        class FakeBus:
            def __init__(self):
                self.seen_cwd = "unset"

            async def collect(self, *, cwd=None):
                self.seen_cwd = cwd
                return "<system-reminder>\nhi\n</system-reminder>"

        bus = FakeBus()
        out = asyncio.run(PromptBuilder._make_reminders(bus, "/work"))
        assert out == "<system-reminder>\nhi\n</system-reminder>"
        assert bus.seen_cwd == "/work"

    def test_blank_cwd_passed_as_none(self):
        import asyncio

        class FakeBus:
            def __init__(self):
                self.seen_cwd = "unset"

            async def collect(self, *, cwd=None):
                self.seen_cwd = cwd
                return ""

        bus = FakeBus()
        asyncio.run(PromptBuilder._make_reminders(bus, ""))
        assert bus.seen_cwd is None


class TestMakeLanguage:
    def test_empty_when_none(self):
        assert PromptBuilder._make_language(None) == ""
        assert PromptBuilder._make_language("") == ""

    def test_substitutes_language_name(self):
        out = PromptBuilder._make_language("Chinese")
        assert "Chinese" in out
        assert "${language_name}" not in out


class TestMakeScratchpad:
    def test_empty_when_none(self):
        assert PromptBuilder._make_scratchpad(None) == ""

    def test_substitutes_dir(self):
        out = PromptBuilder._make_scratchpad("/scratch")
        assert "/scratch" in out
        assert "${scratchpad_dir}" not in out


class TestMakeCompactionSections:
    def test_inactive_returns_empty_pair(self):
        cfg = make_config(compaction_enabled=False)
        assert PromptBuilder._make_compaction_sections(cfg) == ("", "")

    def test_active_emits_all_sections(self):
        cfg = make_config(
            compaction_enabled=True,
            protected_recent_messages=5,
        )
        frc, final_output = PromptBuilder._make_compaction_sections(cfg)
        assert "5" in frc  # keep_recent substituted
        assert "${keep_recent}" not in frc
        assert final_output == R.TASK_FINAL_OUTPUT_SECTION


class TestMakeEnvSection:
    def test_contains_model_and_header(self):
        out = PromptBuilder._make_env_section("claude-x", working_dir="/work")
        assert "claude-x" in out
        assert "# Environment" in out

    def test_renders_project_directory(self):
        out = PromptBuilder._make_env_section("m", working_dir="/w")
        assert "Project directory: /w" in out


class TestMakeSkillsGuide:
    """The system prompt now carries only the static Skill Loading Guide; the
    volatile index migrated to the per-turn SkillListingContextSource
    (see ztest/turn_context/test_skill_listing)."""

    def test_no_injector_returns_empty(self):
        assert PromptBuilder._make_skills_guide(FakeSkillManager(injector=None), True) == ""

    def test_with_injector_returns_guide(self):
        inj = FakeInjector(guide="SKILL_GUIDE")
        out = PromptBuilder._make_skills_guide(FakeSkillManager(injector=inj), True)
        assert out == "SKILL_GUIDE"

    def test_disabled_returns_empty_even_with_injector(self):
        inj = FakeInjector(guide="SKILL_GUIDE")
        out = PromptBuilder._make_skills_guide(FakeSkillManager(injector=inj), False)
        assert out == ""


# --------------------------------------------------------------------------
# collect_context integration
# --------------------------------------------------------------------------
class TestCollectContext:
    def _subsystems(self, **overrides):
        return ThinkSubsystems(
            config=overrides.get("config", make_config()),
            model_name=overrides.get("model_name", "test-model"),
            executor=overrides.get("executor", FakeExecutor()),
            skill_manager=overrides.get("skill_manager", FakeSkillManager()),
            command_channel=overrides.get("command_channel"),
        )

    def test_basic_assembly(self):
        inputs = ThinkInputs(working_dir="/work")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert isinstance(ctx, ThinkContext)
        assert ctx.working_dir == "/work"

    # The volatile tool catalog is no longer built into the system prompt /
    # ThinkContext — it rides the per-turn ToolCatalogContextSource (XML) or the
    # API ``tools=`` param (native). See ztest/turn_context/test_tool_catalog.py.

    def test_memory_populated_when_dir_set(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("idx-line", encoding="utf-8")
        inputs = ThinkInputs(memory_dir=tmp_path)
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert ctx.memory  # instructions present
        assert "idx-line" in ctx.memory_context

    def test_memory_empty_without_dir(self):
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems()))
        assert ctx.memory == ""
        assert ctx.memory_context == ""

    def test_language_and_scratchpad(self):
        # Language now flows from config.models.response_language, not ThinkInputs.
        inputs = ThinkInputs(scratchpad_dir="/sp")
        cfg = make_config(response_language="Chinese")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems(config=cfg)))
        assert "Chinese" in ctx.language
        assert "/sp" in ctx.scratchpad

    def test_prompt_vars_from_channel(self):
        # A channel supplies its protocol fills; collect_context copies them
        # verbatim into ctx.prompt_vars (the single-source path).
        channel = _FakeChannel({k: "FILL-" + k for k in PROMPT_VAR_KEYS})
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems(command_channel=channel)))
        assert ctx.prompt_vars == {k: "FILL-" + k for k in PROMPT_VAR_KEYS}

    def test_prompt_vars_default_when_no_channel(self):
        # No channel -> ctx keeps the empty-string defaults (nothing overrides).
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems()))
        assert ctx.prompt_vars == {k: "" for k in PROMPT_VAR_KEYS}

    def test_partial_prompt_vars_rejected(self):
        # A channel that drops a required key would leak a literal ${...}; the
        # completeness guard raises instead.
        channel = _FakeChannel({"bogus": "X"})  # missing the required key(s)
        with pytest.raises(ValueError, match="missing required keys"):
            run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems(command_channel=channel)))

    def test_reminders_stub_empty(self):
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems()))
        assert ctx.reminders == ""

    def test_compaction_sections_active(self):
        cfg = make_config(compaction_enabled=True)
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems(config=cfg)))
        assert ctx.frc
        assert ctx.task_final_output

    def test_end_to_end_build_from_collected_context(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("mem-idx", encoding="utf-8")
        inputs = ThinkInputs(working_dir="/work", memory_dir=tmp_path)
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        sys_p, usr_p = PromptBuilder.build(R.SYSTEM_PROMPT, R.CMD_PROMPT, ctx)
        assert R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in sys_p
        assert usr_p.startswith("# MEMORY.md")
        # cwd no longer rides the tail; the env block in the system prompt carries
        # the startup dir (the stable base), with no per-turn cwd reminder.
        assert "current directory" not in usr_p
