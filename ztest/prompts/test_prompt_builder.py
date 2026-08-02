#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.kernel.inference.prompt_builder — PromptBuilder + InferenceContext.

PromptBuilder is a stateless assembler: every method is a pure function over
InferenceInputs / InferenceContext / the four subsystems. The tests cover the system &
user prompt assembly (placeholder substitution + cache-boundary removal +
memory/reminder injection), each ``_make_*`` section builder, and the full
collect_context() integration through the duck-typed fakes in conftest.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.kernel.commands.channel import PROMPT_VAR_KEYS
from mote.kernel.inference import prompts as R
from mote.kernel.inference.prompt_builder import InferenceContext, InferenceInputs, InferenceSubsystems, PromptBuilder

from .conftest import FakeExecutor, FakeSkillManager, make_config


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

    def vocabulary(self) -> dict:
        return {}

    def wants_tool_catalog(self) -> bool:
        return False


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------
class TestDataclasses:
    def test_think_inputs_defaults(self):
        ti = InferenceInputs()
        assert ti.working_dir == ""
        assert ti.memory_dir is None

    def test_think_context_defaults(self):
        tc = InferenceContext()
        assert tc.env_section == ""
        # The protocol fills default to empty strings until a channel supplies them.
        assert tc.prompt_vars == {k: "" for k in PROMPT_VAR_KEYS}


# --------------------------------------------------------------------------
# join_sections
# --------------------------------------------------------------------------
class TestRoleInfo:
    def test_role_info_flows_into_system_prompt(self):
        # role_info is a static schema string carried straight through: it lands
        # in ctx.role_info and substitutes into the ${role_info} placeholder.
        ctx = InferenceContext(role_info="# My charter\nDo the thing.")
        sys_p = PromptBuilder._build_system_prompt(R.SYSTEM_PROMPT, ctx)
        assert "# My charter" in sys_p
        assert "${role_info}" not in sys_p

    def test_empty_role_info_leaves_no_placeholder(self):
        ctx = InferenceContext(role_info="")
        sys_p = PromptBuilder._build_system_prompt(R.SYSTEM_PROMPT, ctx)
        assert "${role_info}" not in sys_p


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
        ctx = InferenceContext(env_section="ENVBLOCK")
        sys_p = PromptBuilder._build_system_prompt(R.SYSTEM_PROMPT, ctx)
        assert "ENVBLOCK" in sys_p
        # boundary marker removed
        assert R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in sys_p
        # no unresolved placeholders for the keys we mapped
        assert "${env_section}" not in sys_p

    def test_static_toolset_instructions_render_in_system_prompt(self):
        ctx = InferenceContext(tool_instructions="# Toolset instructions\nStay scoped.")
        sys_p = PromptBuilder._build_system_prompt(R.SYSTEM_PROMPT, ctx)
        assert "# Toolset instructions\nStay scoped." in sys_p

    def test_missing_placeholder_tolerated(self):
        # safe_substitute: a template with an unknown $foo is left intact, no raise.
        ctx = InferenceContext()
        out = PromptBuilder._build_system_prompt("hello $unknown ${env_section}", ctx)
        assert "$unknown" in out


class TestBuildUserPrompt:
    def test_empty_base_yields_empty_when_no_context(self):
        # cwd + timestamp moved off the tail into per-turn reminder sources and the
        # base template is now empty, so with no memory/reminders the tail is empty
        # — no dangling "# Current State" header, no "current directory" line.
        ctx = InferenceContext(working_dir="/work")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out == ""
        assert "current directory" not in out
        assert "Current State" not in out

    def test_prepends_memory_context(self):
        ctx = InferenceContext(working_dir="/w", memory_context="# MEMORY.md\nidx")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out.startswith("# MEMORY.md\nidx")

    def test_appends_reminders(self):
        ctx = InferenceContext(working_dir="/w", reminders="REMIND")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out.rstrip().endswith("REMIND")

    def test_memory_and_reminders_together(self):
        ctx = InferenceContext(working_dir="/w", memory_context="MEM", reminders="REM")
        out = PromptBuilder._build_user_prompt(R.CMD_PROMPT, ctx)
        assert out.startswith("MEM")
        assert out.rstrip().endswith("REM")


class TestBuildTuple:
    def test_build_returns_pair(self):
        ctx = InferenceContext(env_section="ENVBLOCK", working_dir="/w")
        sys_p, usr_p = PromptBuilder.build(R.SYSTEM_PROMPT, R.CMD_PROMPT, ctx)
        assert "ENVBLOCK" in sys_p
        assert isinstance(usr_p, str)


# --------------------------------------------------------------------------
# Substitution maps
# --------------------------------------------------------------------------
class TestSubstitutionMaps:
    def test_system_substitutions_keys(self):
        # XML built-ins have one system-prompt slot; MCP/pipeline definitions use
        # the reminder catalog. Protocol sections come from ctx.prompt_vars.
        ctx = InferenceContext(
            env_section="ENVBLOCK",
            prompt_vars={"command_guide": "CG", "tool_usage_guide": "TUG"},
        )
        d = PromptBuilder._system_substitutions(ctx)
        assert d["env_section"] == "ENVBLOCK"
        assert d["command_guide"] == "CG"
        assert d["tool_usage_guide"] == "TUG"
        assert d["tool_catalog"] == ""
        assert d["tool_instructions"] == ""

    def test_user_substitutions_keys(self):
        # current_state is now empty: cwd + time moved to per-turn reminder sources.
        ctx = InferenceContext(working_dir="/here")
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
        from mote.kernel.inference.memory_prompts import MEMORY_EMPTY_STATE

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
    def test_none_turn_context_bus_returns_empty(self):
        import asyncio

        assert asyncio.run(PromptBuilder._make_reminders(None, "/work")) == ""

    def test_delegates_to_turn_context_bus_collect(self):
        import asyncio

        class FakeTurnContextBus:
            def __init__(self):
                self.seen_cwd = "unset"

            async def collect(self, *, cwd=None):
                self.seen_cwd = cwd
                return "<system-reminder>\nhi\n</system-reminder>"

        bus = FakeTurnContextBus()
        out = asyncio.run(PromptBuilder._make_reminders(bus, "/work"))
        assert out == "<system-reminder>\nhi\n</system-reminder>"
        assert bus.seen_cwd == "/work"

    def test_blank_cwd_passed_as_none(self):
        import asyncio

        class FakeTurnContextBus:
            def __init__(self):
                self.seen_cwd = "unset"

            async def collect(self, *, cwd=None):
                self.seen_cwd = cwd
                return ""

        bus = FakeTurnContextBus()
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


class TestMakeCompactionSection:
    def test_inactive_returns_empty(self):
        cfg = make_config(compaction_enabled=False)
        assert PromptBuilder._make_compaction_section(cfg) == ""

    def test_active_emits_merged_section(self):
        cfg = make_config(
            compaction_enabled=True,
            protected_recent_messages=5,
        )
        section = PromptBuilder._make_compaction_section(cfg)
        assert "5" in section  # keep_recent substituted
        assert "${keep_recent}" not in section
        # The merged section carries both the mid-loop note guidance and the
        # end-of-task durable-record contract (with its <lesson> tag).
        assert "# Surviving compaction" in section
        assert "<lesson>" in section


class TestMakeEnvSection:
    def test_contains_model_and_header(self):
        out = PromptBuilder._make_env_section("claude-x", working_dir="/work")
        assert "claude-x" in out
        assert "# Environment" in out

    def test_renders_project_directory(self):
        out = PromptBuilder._make_env_section("m", working_dir="/w")
        assert "Project directory: /w" in out


# --------------------------------------------------------------------------
# collect_context integration
# --------------------------------------------------------------------------
class TestCollectContext:
    def _subsystems(self, **overrides):
        config = overrides.get("config", make_config())
        return InferenceSubsystems(
            config=config,
            model_name=overrides.get("model_name", "test-model"),
            response_language=overrides.get("response_language", config.models.response_language),
            executor=overrides.get("executor", FakeExecutor()),
            command_channel=overrides.get("command_channel"),
        )

    def test_basic_assembly(self):
        inputs = InferenceInputs(working_dir="/work")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert isinstance(ctx, InferenceContext)
        assert ctx.working_dir == "/work"

    def test_xml_builtins_populate_system_prompt_catalog(self):
        from mote.kernel.commands.xml.channel import XmlCommandChannel

        executor = FakeExecutor(tools={"Read": {"name": "Read"}})
        ctx = run(
            PromptBuilder.collect_context(
                InferenceInputs(),
                self._subsystems(executor=executor, command_channel=XmlCommandChannel()),
            )
        )
        assert "# Available Commands" in ctx.tool_catalog
        assert '"Read"' in ctx.tool_catalog

    def test_static_toolset_instructions_populate_system_prompt_section(self):
        executor = FakeExecutor(instructions=("Stay inside the workspace.",))
        ctx = run(
            PromptBuilder.collect_context(
                InferenceInputs(),
                self._subsystems(executor=executor),
            )
        )
        assert ctx.tool_instructions == ("# Toolset instructions\nStay inside the workspace.")

    def test_memory_populated_when_dir_set(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("idx-line", encoding="utf-8")
        inputs = InferenceInputs(memory_dir=tmp_path)
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert ctx.memory  # instructions present
        assert "idx-line" in ctx.memory_context

    def test_memory_empty_without_dir(self):
        ctx = run(PromptBuilder.collect_context(InferenceInputs(), self._subsystems()))
        assert ctx.memory == ""
        assert ctx.memory_context == ""

    def test_language_and_scratchpad(self):
        # Language now flows from config.models.response_language, not InferenceInputs.
        inputs = InferenceInputs(scratchpad_dir="/sp")
        cfg = make_config(response_language="Chinese")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems(config=cfg)))
        assert "Chinese" in ctx.language
        assert "/sp" in ctx.scratchpad

    def test_prompt_vars_from_channel(self):
        # A channel supplies its protocol fills; collect_context copies them
        # verbatim into ctx.prompt_vars (the single-source path).
        channel = _FakeChannel({k: "FILL-" + k for k in PROMPT_VAR_KEYS})
        ctx = run(PromptBuilder.collect_context(InferenceInputs(), self._subsystems(command_channel=channel)))
        assert ctx.prompt_vars == {k: "FILL-" + k for k in PROMPT_VAR_KEYS}

    def test_prompt_vars_default_when_no_channel(self):
        # No channel -> ctx keeps the empty-string defaults (nothing overrides).
        ctx = run(PromptBuilder.collect_context(InferenceInputs(), self._subsystems()))
        assert ctx.prompt_vars == {k: "" for k in PROMPT_VAR_KEYS}

    def test_partial_prompt_vars_rejected(self):
        # A channel that drops a required key would leak a literal ${...}; the
        # completeness guard raises instead.
        channel = _FakeChannel({"bogus": "X"})  # missing the required key(s)
        with pytest.raises(ValueError, match="missing required keys"):
            run(PromptBuilder.collect_context(InferenceInputs(), self._subsystems(command_channel=channel)))

    def test_reminders_stub_empty(self):
        ctx = run(PromptBuilder.collect_context(InferenceInputs(), self._subsystems()))
        assert ctx.reminders == ""

    def test_role_info_carried_from_inputs(self):
        inputs = InferenceInputs(role_info="# Charter\nBe helpful.")
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        assert ctx.role_info == "# Charter\nBe helpful."

    def test_compaction_section_active(self):
        cfg = make_config(compaction_enabled=True)
        ctx = run(PromptBuilder.collect_context(InferenceInputs(), self._subsystems(config=cfg)))
        assert ctx.compaction

    def test_end_to_end_build_from_collected_context(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("mem-idx", encoding="utf-8")
        inputs = InferenceInputs(working_dir="/work", memory_dir=tmp_path)
        ctx = run(PromptBuilder.collect_context(inputs, self._subsystems()))
        sys_p, usr_p = PromptBuilder.build(R.SYSTEM_PROMPT, R.CMD_PROMPT, ctx)
        assert R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in sys_p
        assert usr_p.startswith("# MEMORY.md")
        # cwd no longer rides the tail; the env block in the system prompt carries
        # the startup dir (the stable base), with no per-turn cwd reminder.
        assert "current directory" not in usr_p
