"""
PromptBuilder — pure-function prompt assembly for the Role react loop.

ThinkContext is a pure dataclass holding all data needed for one think() cycle.
PromptBuilder is a stateless assembler that turns ThinkContext into prompts.

Usage:
    ctx = await PromptBuilder.collect_context(role.think_inputs(), role.think_subsystems())
    system_prompt, user_prompt = PromptBuilder.build(system_tpl, cmd_tpl, ctx)
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass, field
from string import Template
from typing import Any, Optional

from metagpt.common.const import DEFAULT_WORKSPACE_ROOT
from metagpt.common.prompt.role import (
    CONSTRAINT_TEMPLATE,
    FRC_SECTION,
    LANGUAGE_SECTION,
    MGX_INFO,
    PREFIX_TEMPLATE,
    SCRATCHPAD_SECTION,
    SUMMARIZE_TOOL_RESULTS_SECTION,
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
)

from metagpt.common.prompt.memory import MEMORY_CONTEXT, MEMORY_EMPTY_STATE, MEMORY_INSTRUCTIONS
from metagpt.common.utils.role_zero_utils import get_time_info


@dataclass
class ThinkInputs:
    """The field set a Role publishes for one think() cycle — pure data only.

    Symmetric to Role.tool_capabilities(): a single explicit bundle the Role
    hands to PromptBuilder, so collect_context depends on this stable shape
    rather than reaching into the Role. Every field is a flat, pre-derived value
    the Role pushes (identity strings, env clause, team listing, cwd, etc.); no
    nested Role objects leak through. No live collaborators here — those live in
    ThinkSubsystems.
    """

    # Identity (flattened from RoleSchema so the Role pushes explicit values
    # instead of handing over a nested schema object).
    name: str = ""
    profile: str = ""
    goal: str = ""
    constraints: str = ""
    desc: str = ""
    example: str = ""
    instruction: str = ""

    env_desc: str = ""
    other_role_names: str = ""
    team_info: str = ""
    working_dir: str = ""
    project_root: Any = None
    output_format: Optional[str] = None
    command_guide: Optional[str] = None
    memory_dir: Any = None
    language: Optional[str] = None
    scratchpad_dir: Any = None


@dataclass
class ThinkSubsystems:
    """The live collaborators a Role hands to PromptBuilder for one think() cycle.

    Counterpart to ThinkInputs: where ThinkInputs is a pure-data snapshot, this
    bundles the active objects PromptBuilder queries (config, llm, executor,
    skill_manager). Kept separate so the data/behavior split stays
    as clean as ThinkContext (data) vs the subsystems (behavior).
    """

    config: Any
    llm: Any
    executor: Any
    skill_manager: Any
    # The unified per-turn ephemeral-context bus (git/token/bg-tasks/LSP feeds).
    # None => no ephemeral context this cycle (the Role didn't wire a bus).
    turn_context_bus: Any = None


@dataclass
class ThinkContext:
    """All data needed to assemble prompts for one think() cycle.

    Pure data container — no classmethods, no Role access.
    Populated by Role._collect_think_context().
    """

    # Identity & domain (rendered; role_prefix/role_info are spliced from
    # ThinkInputs by build_role_prefix / build_role_info).
    role_info: str = ""
    role_prefix: str = ""
    domain_info: str = ""
    example: str = ""
    instruction: str = ""

    # Tools
    tool_info: str = ""       # built-in tools (rendered JSON for ${available_commands})
    mcp_info: str = ""        # MCP tools (rendered JSON for ${mcp_tools})

    # Environment
    env_section: str = ""

    # Skills
    skills_info: str = ""

    # Dynamic system-prompt sections (below the cache boundary). Each is the
    # fully-rendered section text, or "" when the feature is inactive.
    memory: str = ""
    language: str = ""
    scratchpad: str = ""
    frc: str = ""
    summarize_tool_results: str = ""

    # Output command-block format (parser contract). Defaults to "" (no format
    # section); the command channel supplies OUTPUT_SECTION for the XML protocol
    # via ThinkInputs.output_format.
    output_format: str = ""

    # "# Using commands" section. Defaults to "" (no section); the command
    # channel supplies the protocol-specific guidance via ThinkInputs.command_guide
    # (XML <end></end> mechanics vs native tool-call mechanics).
    command_guide: str = ""

    # MEMORY.md content injected into the user prompt (CC injects the index via
    # user context so a changing index never busts the system-prompt cache).
    memory_context: str = ""

    # Proactive framework reminders injected into the user prompt each turn —
    # the "secretary" hook. Reserved seam: a future reminder subsystem fills
    # this based on the current situation (token pressure via
    # ContextManager.token_state(), idle tools, finished background tasks,
    # changed files, ...). Injected like memory_context — per-turn user context,
    # never the cacheable system prompt, never stored in history. "" => none.
    reminders: str = ""

    # User prompt
    working_dir: str = ""

    # State data passed to exp_cache
    state_data: dict = field(default_factory=dict)


class PromptBuilder:
    """Stateless prompt assembler.

    Takes template strings and a ThinkContext (for data),
    returns (system_prompt, user_prompt) ready for LLM.
    """

    @staticmethod
    def build(system_tpl: str, cmd_tpl: str, ctx: ThinkContext) -> tuple[str, str]:
        """Assemble system_prompt and user_prompt from metagpt.context."""
        system_prompt = PromptBuilder._build_system_prompt(system_tpl, ctx)
        user_prompt = PromptBuilder._build_user_prompt(cmd_tpl, ctx)
        return system_prompt, user_prompt

    @staticmethod
    def pick_summary_prompt(*, summary_prompt: str, recommend_prompt: str, need_recommend: bool) -> str:
        """Return the session-summary prompt text for this run.

        Picks the recommend-tagged prompt when ``need_recommend`` is set,
        otherwise the plain summary prompt. Returns the prompt string only —
        wrapping it into a message and appending to history is the Role's job
        (the message-envelope concern lives in the Role, not the builder).
        """
        return recommend_prompt if need_recommend else summary_prompt

    @staticmethod
    def build_role_prefix(inputs: ThinkInputs) -> str:
        """Render the role prefix (identity + optional env clause) from inputs.

        Mirrors the old Role._get_role_prefix. When ``desc`` is set it wins
        verbatim; otherwise the prefix is rendered from PREFIX_TEMPLATE (plus
        CONSTRAINT_TEMPLATE when constraints exist). When the role lives in a
        described env, an env clause is appended.
        """
        if inputs.desc:
            return inputs.desc
        prefix = Template(PREFIX_TEMPLATE).safe_substitute(
            profile=inputs.profile, name=inputs.name, goal=inputs.goal
        )
        if inputs.constraints:
            prefix += Template(CONSTRAINT_TEMPLATE).safe_substitute(constraints=inputs.constraints)
        if inputs.env_desc:
            prefix += f"You are in {inputs.env_desc} with roles({inputs.other_role_names})."
        return prefix

    @staticmethod
    def build_role_info(role_prefix: str, team_info: str) -> str:
        """Combine the role prefix with the team-member listing (if any)."""
        if not team_info:
            return role_prefix
        return f"{role_prefix}\nYour team member:\n{team_info}"

    @staticmethod
    def _system_substitutions(ctx: ThinkContext) -> dict:
        """Map ThinkContext fields to the system template's $placeholders."""
        return dict(
            role_info=ctx.role_info,
            domain_info=ctx.domain_info,
            available_commands=ctx.tool_info,
            mcp_tools=ctx.mcp_info,
            example=ctx.example,
            instruction=ctx.instruction,
            env_section=ctx.env_section,
            skills_info=ctx.skills_info,
            memory=ctx.memory,
            language=ctx.language,
            scratchpad=ctx.scratchpad,
            frc=ctx.frc,
            summarize_tool_results=ctx.summarize_tool_results,
            output_format=ctx.output_format,
            command_guide=ctx.command_guide,
        )

    @staticmethod
    def _user_substitutions(ctx: ThinkContext) -> dict:
        """Map ThinkContext fields to the command template's $placeholders."""
        return dict(
            current_state=f"current directory: {ctx.working_dir}",
        )

    @staticmethod
    def _build_system_prompt(system_tpl: str, ctx: ThinkContext) -> str:
        # safe_substitute tolerates missing/extra placeholders (no KeyError) and
        # does not treat literal braces in the prompt (JSON/CSS/XML) as fields.
        #
        # The template carries SYSTEM_PROMPT_DYNAMIC_BOUNDARY between the static
        # (cacheable) prefix and the dynamic region. We substitute the whole
        # template, then drop the marker line so it never reaches the model. The
        # split point stays a real, stable boundary for future prompt-caching:
        # nothing above it contains a $placeholder, so the prefix is byte-stable.
        rendered = Template(system_tpl).safe_substitute(PromptBuilder._system_substitutions(ctx))
        return rendered.replace(SYSTEM_PROMPT_DYNAMIC_BOUNDARY + "\n", "").replace(
            SYSTEM_PROMPT_DYNAMIC_BOUNDARY, ""
        )

    @staticmethod
    def _build_user_prompt(cmd_tpl: str, ctx: ThinkContext) -> str:
        rendered = Template(cmd_tpl).safe_substitute(PromptBuilder._user_substitutions(ctx))
        # MEMORY.md is injected via user context (not the system prompt) so a
        # changing index never busts the cacheable system-prompt prefix.
        if ctx.memory_context:
            rendered = f"{ctx.memory_context}\n\n{rendered}"
        # Proactive framework reminders (the "secretary" hook) — appended as
        # per-turn user context, same rationale as memory_context. Empty until a
        # reminder strategy fills ctx.reminders (see _make_reminders).
        if ctx.reminders:
            rendered = f"{rendered}\n\n{ctx.reminders}"
        return rendered

    @staticmethod
    def join_sections(sections: list[str | None]) -> str:
        """Join non-empty sections, dropping None/blank entries.

        Helper for assembling a system prompt from independent section strings
        (Claude Code style) instead of one monolithic template. A section that
        is None or empty/whitespace is skipped entirely.
        """
        return "\n".join(s for s in sections if s and s.strip())


    # ------------------------------------------------------------------
    # Context collection — gathers all data from subsystems into ThinkContext
    # ------------------------------------------------------------------

    @staticmethod
    async def collect_context(inputs: ThinkInputs, subsystems: ThinkSubsystems) -> ThinkContext:
        """Collect all data from subsystems needed for one think() cycle.

        Args:
            inputs: The Role's published field set (RoleSchema + state-derived
                values such as env clause, team listing, cwd, memory_dir, etc.).
            subsystems: The live collaborators PromptBuilder queries (config,
                llm, executor, skill_manager).
        """
        config = subsystems.config

        ctx = ThinkContext()

        # Splice the raw inputs into the rendered identity.
        ctx.role_prefix = PromptBuilder.build_role_prefix(inputs)
        ctx.role_info = PromptBuilder.build_role_info(ctx.role_prefix, inputs.team_info)
        ctx.domain_info = PromptBuilder._make_domain_info(config)
        ctx.example = inputs.example
        ctx.instruction = inputs.instruction.strip()

        ctx.working_dir = inputs.working_dir

        ctx.tool_info = json.dumps(subsystems.executor.get_tool_schemas())
        ctx.mcp_info = json.dumps(subsystems.executor.get_mcp_tool_schemas())
        # The static environment block (cwd / platform / model). Git working-tree
        # state used to be appended here; it now flows through the per-turn
        # ephemeral-context bus (the turn_context layer) and lands in the user
        # prompt's <system-reminder>, so volatile git state never touches this
        # cacheable section.
        ctx.env_section = PromptBuilder._make_env_section(
            subsystems.llm,
            working_dir=ctx.working_dir,
            project_root=inputs.project_root,
        )
        ctx.skills_info = PromptBuilder._make_skills_info(subsystems.skill_manager, config)

        # Dynamic sections (below the cache boundary).
        ctx.memory, ctx.memory_context = PromptBuilder._make_memory(inputs.memory_dir)
        ctx.language = PromptBuilder._make_language(inputs.language)
        ctx.scratchpad = PromptBuilder._make_scratchpad(inputs.scratchpad_dir)
        ctx.frc, ctx.summarize_tool_results = PromptBuilder._make_compaction_sections(config)

        # Per-turn ephemeral context (git / token pressure / background tasks /
        # LSP diagnostics) gathered by the turn_context bus and injected into the
        # user prompt as a <system-reminder>. None bus => "" (nothing injected).
        ctx.reminders = await PromptBuilder._make_reminders(subsystems.turn_context_bus, ctx.working_dir)

        # Output-format section comes from the command channel: XML supplies
        # OUTPUT_SECTION, native tool-use supplies "" (API constrains output).
        # None means "caller didn't override" — keep the ThinkContext default.
        if inputs.output_format is not None:
            ctx.output_format = inputs.output_format

        # "# Using commands" guidance, also from the command channel: XML supplies
        # the <end></end> / command-tag mechanics, native supplies tool-call
        # mechanics. None means "caller didn't override" — keep the default "".
        if inputs.command_guide is not None:
            ctx.command_guide = inputs.command_guide

        ctx.state_data = dict(instruction=ctx.instruction)
        return ctx

    @staticmethod
    def _make_memory(memory_dir) -> tuple[str, str]:
        """Build the # Memory system section and the MEMORY.md user-context block.

        Returns ("", "") when no memory_dir is configured. The instructions are
        static (cacheable); the MEMORY.md content is injected via user context so
        a changing index never busts the system-prompt cache prefix.
        """
        if not memory_dir:
            return "", ""
        instructions = Template(MEMORY_INSTRUCTIONS).safe_substitute(memory_dir=str(memory_dir))
        content = PromptBuilder._read_memory_index(memory_dir) or MEMORY_EMPTY_STATE
        context = Template(MEMORY_CONTEXT).safe_substitute(memory_content=content)
        return instructions, context

    @staticmethod
    def _read_memory_index(memory_dir) -> str:
        """Read MEMORY.md from memory_dir, or "" if absent/unreadable."""
        try:
            path = os.path.join(str(memory_dir), "MEMORY.md")
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except (OSError, ValueError):
            return ""

    @staticmethod
    async def _make_reminders(turn_context_bus, cwd: str = "") -> str:
        """Gather per-turn ephemeral context from the turn_context bus.

        Returns the merged <system-reminder> block that _build_user_prompt
        appends to the user prompt, or "" when no bus is wired. The bus polls
        its EphemeralContextSource feeds (git status, token pressure, background
        tasks, LSP diagnostics, ...) and merges the non-empty blocks.

        The output is ephemeral by design (user context, not the cacheable
        system prompt, never stored in history), mirroring memory_context.
        """
        if turn_context_bus is None:
            return ""
        return await turn_context_bus.collect(cwd=cwd or None)

    @staticmethod
    def _make_language(language) -> str:
        if not language:
            return ""
        return Template(LANGUAGE_SECTION).safe_substitute(language_name=str(language))

    @staticmethod
    def _make_scratchpad(scratchpad_dir) -> str:
        if not scratchpad_dir:
            return ""
        return Template(SCRATCHPAD_SECTION).safe_substitute(scratchpad_dir=str(scratchpad_dir))

    @staticmethod
    def _make_compaction_sections(config) -> tuple[str, str]:
        """Build the # Function Result Clearing and tool-results sections.

        Emitted only when adaptive (token-based) compaction is active, since
        that is what actually clears old tool results from metagpt.context. keep_recent
        comes from protected_recent_messages. Returns ("", "") otherwise.
        """
        rz = config.role_zero
        active = getattr(rz, "enable_compressable_memory", False) and getattr(
            rz, "compress_type", ""
        ) == "compaction"
        if not active:
            return "", ""
        keep_recent = getattr(rz, "protected_recent_messages", 8)
        frc = Template(FRC_SECTION).safe_substitute(keep_recent=str(keep_recent))
        return frc, SUMMARIZE_TOOL_RESULTS_SECTION

    @staticmethod
    def _make_domain_info(config) -> str:
        return Template(MGX_INFO).safe_substitute(
            ai_capability_models=", ".join(config.role_zero.ai_capability_models),
        )

    @staticmethod
    def _make_env_section(llm, working_dir: str = "", project_root=None) -> str:
        cwd = working_dir or str(DEFAULT_WORKSPACE_ROOT)
        root = str(project_root) if project_root else str(DEFAULT_WORKSPACE_ROOT)
        lines = [
            "# Environment",
            "You have been invoked in the following environment:",
            f" - {get_time_info()}",
            f" - Primary working directory: {cwd}",
            f" - Project root: {root}",
            f" - Platform: {sys.platform}",
            f" - Shell: {os.environ.get('SHELL', '')}",
            f" - OS Version: {platform.platform()}",
            f" - You are powered by the model named {llm.model}.",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _make_skills_info(skill_manager, config) -> str:
        if skill_manager.injector:
            return skill_manager.injector.build_content(
                max_tokens=config.role_zero.max_skill_tokens,
            )
        return ""
