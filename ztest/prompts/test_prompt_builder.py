#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.think.prompt_builder — PromptBuilder + ThinkContext.

PromptBuilder is a stateless assembler: every method is a pure function over
ThinkInputs / ThinkContext / the four subsystems. The tests cover the identity
splice (build_role_prefix / build_role_info), the system & user prompt
assembly (placeholder substitution + cache-boundary removal + memory/reminder
injection), each ``_make_*`` section builder, and the full collect_context()
integration through the duck-typed fakes in conftest.
"""
from __future__ import annotations

import asyncio

from metagpt.common import prompt as R
from metagpt.think.prompt_builder import (
    PromptBuilder,
    ThinkContext,
    ThinkInputs,
    ThinkSubsystems,
)

from .conftest import FakeExecutor, FakeInjector, FakeLLM, FakeSkillManager, make_config


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------
class TestDataclasses:
    def test_think_inputs_defaults(self):
        ti = ThinkInputs()
        assert ti.name == "" and ti.profile == "" and ti.goal == ""
        assert ti.output_format is None
        assert ti.memory_dir is None

    def test_think_context_defaults(self):
        tc = ThinkContext()
        assert tc.role_info == "" and tc.output_format == ""
        assert tc.state_data == {}


# --------------------------------------------------------------------------
# Identity splice
# --------------------------------------------------------------------------
class TestBuildRolePrefix:
    def test_desc_wins_verbatim(self):
        ti = ThinkInputs(desc="I am the boss.", profile="X", name="Y", goal="Z")
        assert PromptBuilder.build_role_prefix(ti) == "I am the boss."

    def test_renders_from_prefix_template(self):
        ti = ThinkInputs(profile="Engineer", name="Bob", goal="ship")
        out = PromptBuilder.build_role_prefix(ti)
        assert "Engineer" in out and "Bob" in out and "ship" in out

    def test_appends_constraints(self):
        ti = ThinkInputs(profile="E", name="B", goal="g", constraints="be terse")
        out = PromptBuilder.build_role_prefix(ti)
        assert "be terse" in out

    def test_appends_env_clause(self):
        ti = ThinkInputs(
            profile="E", name="B", goal="g", env_desc="the office", other_role_names="Carol"
        )
        out = PromptBuilder.build_role_prefix(ti)
        assert "the office" in out
        assert "Carol" in out

    def test_no_env_clause_without_env_desc(self):
        ti = ThinkInputs(profile="E", name="B", goal="g")
        assert "You are in" not in PromptBuilder.build_role_prefix(ti)


class TestBuildRoleInfo:
    def test_no_team_returns_prefix(self):
        assert PromptBuilder.build_role_info("PREFIX", "") == "PREFIX"

    def test_with_team_appends_listing(self):
        out = PromptBuilder.build_role_info("PREFIX", "- Bob: Eng")
        assert out.startswith("PREFIX")
        assert "Your team member:" in out
        assert "- Bob: Eng" in out


# --------------------------------------------------------------------------
# Summary prompt picker
# --------------------------------------------------------------------------
class TestPickSummaryPrompt:
    def test_picks_recommend_when_needed(self):
        out = PromptBuilder.pick_summary_prompt(
            summary_prompt="plain", recommend_prompt="rec", need_recommend=True
        )
        assert out == "rec"

    def test_picks_plain_otherwise(self):
        out = PromptBuilder.pick_summary_prompt(
            summary_prompt="plain", recommend_prompt="rec", need_recommend=False
        )
        assert out == "plain"


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
        ctx = ThinkContext(role_info="ROLE", domain_info="DOM", tool_info="[]", mcp_info="[]")
        sys_p = PromptBuilder._build_system_prompt(R.SYSTEM_PROMPT, ctx)
        assert "ROLE" in sys_p
        assert "DOM" in sys_p
        # boundary marker removed
        assert R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in sys_p
        # no unresolved placeholders for the keys we mapped
        assert "${role_info}" not in sys_p

    def test_missing_placeholder_tolerated(self):
        # safe_substitute: a template with an unknown $foo is left intact, no raise.
        ctx = ThinkContext()
        out = PromptBuilder._build_system_prompt("hello $unknown ${role_info}", ctx)
        assert "$unknown" in out


class TestBuildUserPrompt:
    def test_substitutes_current_state(self):
        ctx = ThinkContext(working_dir="/work")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert "current directory: /work" in out

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
        ctx = ThinkContext(role_info="ROLE", working_dir="/w")
        sys_p, usr_p = PromptBuilder.build(R.SYSTEM_PROMPT, R.CMD_PROMPT, ctx)
        assert "ROLE" in sys_p
        assert "current directory: /w" in usr_p


# --------------------------------------------------------------------------
# Substitution maps
# --------------------------------------------------------------------------
class TestSubstitutionMaps:
    def test_system_substitutions_keys(self):
        ctx = ThinkContext(role_info="r", tool_info="t", mcp_info="m")
        d = PromptBuilder._system_substitutions(ctx)
        assert d["role_info"] == "r"
        assert d["available_commands"] == "t"
        assert d["mcp_tools"] == "m"

    def test_user_substitutions_keys(self):
        ctx = ThinkContext(working_dir="/here")
        d = PromptBuilder._user_substitutions(ctx)
        assert d["current_state"] == "current directory: /here"


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
        from metagpt.common.prompt.memory import MEMORY_EMPTY_STATE

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
        cfg = make_config(enable_compressable_memory=False)
        assert PromptBuilder._make_compaction_sections(cfg) == ("", "")

    def test_inactive_when_wrong_compress_type(self):
        cfg = make_config(enable_compressable_memory=True, compress_type="post_cut_by_token")
        assert PromptBuilder._make_compaction_sections(cfg) == ("", "")

    def test_active_emits_both_sections(self):
        cfg = make_config(
            enable_compressable_memory=True,
            compress_type="compaction",
            protected_recent_messages=5,
        )
        frc, summarize = PromptBuilder._make_compaction_sections(cfg)
        assert "5" in frc  # keep_recent substituted
        assert "${keep_recent}" not in frc
        assert summarize == R.SUMMARIZE_TOOL_RESULTS_SECTION


class TestMakeDomainInfo:
    def test_joins_models(self):
        cfg = make_config(ai_capability_models=["m1", "m2", "m3"])
        out = PromptBuilder._make_domain_info(cfg)
        assert "m1, m2, m3" in out


class TestMakeEnvSection:
    def test_contains_cwd_and_model(self):
        out = PromptBuilder._make_env_section(FakeLLM(model="claude-x"), working_dir="/work")
        assert "/work" in out
        assert "claude-x" in out
        assert "# Environment" in out

    def test_falls_back_to_default_workspace(self):
        out = PromptBuilder._make_env_section(FakeLLM(), working_dir="")
        assert "Primary working directory:" in out

    def test_uses_project_root(self):
        out = PromptBuilder._make_env_section(FakeLLM(), working_dir="/w", project_root="/proj")
        assert "/proj" in out


class TestMakeSkillsInfo:
    def test_no_injector_returns_empty(self):
        cfg = make_config()
        assert PromptBuilder._make_skills_info(FakeSkillManager(injector=None), cfg) == ""

    def test_with_injector_builds_content(self):
        inj = FakeInjector(content="SKILLS")
        cfg = make_config(max_skill_tokens=1234)
        out = PromptBuilder._make_skills_info(FakeSkillManager(injector=inj), cfg)
        assert out == "SKILLS"
        assert inj.max_tokens_seen == 1234


# --------------------------------------------------------------------------
# collect_context integration
# --------------------------------------------------------------------------
class TestCollectContext:
    def _subsystems(self, **overrides):
        return ThinkSubsystems(
            config=overrides.get("config", make_config()),
            llm=overrides.get("llm", FakeLLM()),
            executor=overrides.get("executor", FakeExecutor()),
            skill_manager=overrides.get("skill_manager", FakeSkillManager()),
        )

    def test_basic_assembly(self):
        inputs = ThinkInputs(profile="Eng", name="Bob", goal="ship", instruction="  do it  ")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert isinstance(ctx, ThinkContext)
        assert "Bob" in ctx.role_info
        assert ctx.instruction == "do it"  # stripped
        assert ctx.state_data == {"instruction": "do it"}

    def test_tool_info_is_json(self):
        executor = FakeExecutor(tools=[{"name": "Read"}], mcp_tools=[{"name": "srv:x"}])
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems(executor=executor)))
        assert ctx.tool_info == '[{"name": "Read"}]'
        assert ctx.mcp_info == '[{"name": "srv:x"}]'

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
        inputs = ThinkInputs(language="Chinese", scratchpad_dir="/sp")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert "Chinese" in ctx.language
        assert "/sp" in ctx.scratchpad

    def test_output_format_override(self):
        inputs = ThinkInputs(output_format="OUTPUT_SECTION")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert ctx.output_format == "OUTPUT_SECTION"

    def test_output_format_none_keeps_default(self):
        inputs = ThinkInputs(output_format=None)
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert ctx.output_format == ""

    def test_reminders_stub_empty(self):
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems()))
        assert ctx.reminders == ""

    def test_compaction_sections_active(self):
        cfg = make_config(enable_compressable_memory=True, compress_type="compaction")
        ctx = run(PromptBuilder.collect_context(ThinkInputs(), self._subsystems(config=cfg)))
        assert ctx.frc
        assert ctx.summarize_tool_results

    def test_end_to_end_build_from_collected_context(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("mem-idx", encoding="utf-8")
        inputs = ThinkInputs(
            profile="Eng", name="Bob", goal="ship", working_dir="/work", memory_dir=tmp_path
        )
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        sys_p, usr_p = PromptBuilder.build(R.SYSTEM_PROMPT, R.CMD_PROMPT, ctx)
        assert "Bob" in sys_p
        assert R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in sys_p
        assert usr_p.startswith("# MEMORY.md")
        assert "current directory: /work" in usr_p
