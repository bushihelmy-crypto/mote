"""
PromptBuilder — pure-function prompt assembly for the Role react loop.

ThinkContext is a pure dataclass holding all data needed for one think() cycle.
PromptBuilder is a stateless assembler that turns ThinkContext into prompts.

Usage:
    ctx = await PromptBuilder.collect_context(role.think_inputs(), role.think_subsystems())
    system_prompt, user_prompt = PromptBuilder.build(system_tpl, cmd_tpl, ctx)
"""

from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass, field
from string import Template
from typing import Any, Optional

from mote.common.base.command_channel import PROMPT_VAR_KEYS
from mote.common.const import DEFAULT_WORKSPACE_ROOT
from mote.common.prompt.memory import MEMORY_CONTEXT, MEMORY_EMPTY_STATE, MEMORY_INSTRUCTIONS
from mote.common.prompt.refs import assert_no_symbols
from mote.common.prompt.role import (
    CONSTRAINT_TEMPLATE,
    FRC_SECTION,
    LANGUAGE_SECTION,
    PREFIX_TEMPLATE,
    SCRATCHPAD_SECTION,
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    TASK_FINAL_OUTPUT_SECTION,
)
from mote.common.prompt.tools import BACKGROUND_PIPELINE_SECTION


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
    # Live cwd (follows `cd`) — feeds the per-turn ephemeral SR, never the
    # cacheable system prompt.
    working_dir: str = ""
    # Startup cwd (never follows `cd`) — the stable dir the system prompt's
    # environment block cites, so a mid-session `cd` can't bust the prefix cache.
    original_working_dir: str = ""
    project_root: Any = None
    memory_dir: Any = None
    language: Optional[str] = None
    scratchpad_dir: Any = None


@dataclass
class ThinkSubsystems:
    """The live collaborators a Role hands to PromptBuilder for one think() cycle.

    Counterpart to ThinkInputs: where ThinkInputs is a pure-data snapshot, this
    bundles the active objects PromptBuilder queries (config, executor,
    skill_manager). Kept separate so the data/behavior split stays
    as clean as ThinkContext (data) vs the subsystems (behavior).

    ``model_name`` is the configured model's display name (for the "powered by"
    line) — not a live LLM handle, so PromptBuilder never resolves an LLM.
    """

    config: Any
    model_name: str
    executor: Any
    skill_manager: Any
    # The unified per-turn ephemeral-context bus (git/token/bg-tasks/LSP feeds).
    # None => no ephemeral context this cycle (the Role didn't wire a bus).
    turn_context_bus: Any = None
    # The active CommandChannel. PromptBuilder calls its ``lower(text)`` at the
    # end of assembly to substitute protocol symbols (``⟦...⟧``) with this
    # protocol's surface syntax — the single place protocol mechanics enter the
    # prompt. None => identity (no lowering), for callers/tests without a channel.
    command_channel: Any = None


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
    team_info: str = ""

    # Environment
    env_section: str = ""

    # Skills — the static loading guide only (the volatile index migrated to the
    # per-turn SkillListingContextSource).
    skills_info: str = ""

    # Dynamic system-prompt sections (below the cache boundary). Each is the
    # fully-rendered section text, or "" when the feature is inactive.
    memory: str = ""
    language: str = ""
    scratchpad: str = ""
    frc: str = ""
    task_final_output: str = ""
    pipeline_section: str = ""

    # Protocol-specific ${placeholder} fills supplied by the active command
    # channel's prompt_vars() — command_guide (system "# Using commands"
    # mechanics). Merged into the system template substitutions; the template
    # consumes only the keys it references (safe_substitute ignores the rest).
    # The single seam for protocol prompt sections (was three ThinkInputs fields
    # + three collect_context override blocks). Defaults to "" for every
    # PROMPT_VAR_KEYS entry when no channel is wired.
    prompt_vars: dict = field(default_factory=lambda: {k: "" for k in PROMPT_VAR_KEYS})

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

    # The active channel's symbol-lowering callable (``channel.lower``). Applied
    # to the fully-assembled system+user prompt at the end of ``build`` so the
    # protocol's surface syntax for each ``⟦symbol⟧`` is the LAST thing inserted.
    # None => identity (no channel wired; used by callers/tests without one).
    lower: Any = None


class PromptBuilder:
    """Stateless prompt assembler.

    Takes template strings and a ThinkContext (for data),
    returns (system_prompt, user_prompt) ready for LLM.
    """

    @staticmethod
    def build(system_tpl: str, cmd_tpl: str, ctx: ThinkContext) -> tuple[str, str]:
        """Assemble system_prompt and user_prompt from mote.context."""
        system_prompt = PromptBuilder._build_system_prompt(system_tpl, ctx)
        user_prompt = PromptBuilder._build_user_prompt(cmd_tpl, ctx)
        # Final step: lower protocol symbols (``⟦...⟧``) to the active channel's
        # surface syntax. Done LAST, over the fully-assembled prompts, so a symbol
        # is rendered identically no matter which section it came from — and a
        # native render can never carry an XML mechanic that the prose never held.
        # assert_no_symbols then guarantees nothing leaks: any unlowered symbol
        # (typo / missing vocabulary entry) raises here at build time instead of
        # reaching the model. No channel wired => skip (identity).
        if ctx.lower is not None:
            system_prompt = ctx.lower(system_prompt)
            user_prompt = ctx.lower(user_prompt)
            assert_no_symbols(system_prompt, where="system_prompt")
            assert_no_symbols(user_prompt, where="user_prompt")
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
        prefix = Template(PREFIX_TEMPLATE).safe_substitute(profile=inputs.profile, name=inputs.name, goal=inputs.goal)
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
    def _section(heading: str, body: str) -> str:
        """Render '{heading}\\n{body}', or '' when body is empty/blank.

        Keeps the section heading with its content so a section with no body
        (e.g. no MCP tools) renders nothing at all — no orphan heading.
        """
        if not body or not body.strip():
            return ""
        return f"{heading}\n{body}"

    @staticmethod
    def _system_substitutions(ctx: ThinkContext) -> dict:
        """Map ThinkContext fields to the system template's $placeholders."""
        return dict(
            role_info=PromptBuilder._section("# Basic Info", ctx.role_info),
            team_info=PromptBuilder._section("# Team", ctx.team_info),
            env_section=ctx.env_section,
            skills_info=ctx.skills_info,
            memory=ctx.memory,
            language=ctx.language,
            scratchpad=ctx.scratchpad,
            frc=ctx.frc,
            task_final_output=ctx.task_final_output,
            pipeline_section=ctx.pipeline_section,
            # command_guide + tool_usage_guide (+ any future protocol section).
            **ctx.prompt_vars,
        )

    @staticmethod
    def _user_substitutions(ctx: ThinkContext) -> dict:
        """Map ThinkContext fields to the command template's $placeholders.

        ``current_state`` is intentionally empty: the wall-clock time it used to
        carry now rides the per-turn ``<system-reminder>`` (TimestampContextSource)
        instead of being hand-spliced onto the request tail. The cwd is not
        re-surfaced per turn at all — it is a stable base cited once in the system
        prompt's env block (see ``_make_env_section``). Consolidating the volatile
        content into the structured reminder envelope keeps this template stable
        and puts all ephemeral tail content in one place.
        """
        return dict(current_state="")

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
        rendered = rendered.replace(SYSTEM_PROMPT_DYNAMIC_BOUNDARY + "\n", "").replace(
            SYSTEM_PROMPT_DYNAMIC_BOUNDARY, ""
        )
        # Inactive sections substitute to "" and leave runs of blank lines behind.
        # Collapse any run of 3+ newlines down to a single blank line and trim the
        # ends. This is idempotent on the already-clean static prefix (its blocks
        # are single-blank-line separated), so the cacheable prefix stays
        # byte-identical across turns.
        return re.sub(r"\n{3,}", "\n\n", rendered).strip() + "\n"

    @staticmethod
    def _build_user_prompt(cmd_tpl: str, ctx: ThinkContext) -> str:
        # The base template is empty by default (the old "# Current State" block
        # moved to per-turn reminder sources), but a custom cmd_prompt is still
        # substituted so role-specific templates keep working.
        rendered = Template(cmd_tpl).safe_substitute(PromptBuilder._user_substitutions(ctx))
        # Assemble the trailing user prompt from its parts, blank-line separated,
        # dropping any that are empty so an empty base template leaves no stray
        # whitespace:
        #   - MEMORY.md (memory_context): injected via user context, not the system
        #     prompt, so a changing index never busts the cacheable system prefix;
        #   - the rendered base template (usually empty);
        #   - the per-turn <system-reminder> envelope (reminders).
        parts = [p for p in (ctx.memory_context, rendered.strip(), ctx.reminders) if p]
        return "\n\n".join(parts)

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
        # role_info loads only the role's desc; empty desc => "" => _section
        # renders nothing (no "# Basic Info" heading).
        ctx.role_info = inputs.desc
        # Team listing (rendered below role_info); empty => no "# Team" section.
        ctx.team_info = inputs.team_info

        ctx.working_dir = inputs.working_dir

        # The volatile tool catalog (built-in / MCP / pipeline schemas) is NOT
        # built into the system prompt. Native tool-use passes every tool via the
        # API ``tools=`` param; XML mode delivers the catalog per-turn through the
        # ephemeral-context bus (ToolCatalogContextSource) so a tool/MCP hot-reload
        # never busts the cacheable prefix. The static orientation on how to call
        # tools rides ${tool_usage_guide} (from the channel's prompt_vars) instead.
        # The static environment block (cwd / platform / model). Git working-tree
        # state used to be appended here; it now flows through the per-turn
        # ephemeral-context bus (the turn_context layer) and lands in the user
        # prompt's <system-reminder>, so volatile git state never touches this
        # cacheable section.
        # The env block cites the *startup* cwd (``original_working_dir``). This is
        # the model's sole cwd anchor: working_dir is a stable relative-path base
        # equal to it (Codex-aligned — never drifts with ``cd``), so there is no
        # separate per-turn cwd reminder to keep in sync, and nothing can bust this
        # cacheable prefix.
        ctx.env_section = PromptBuilder._make_env_section(
            subsystems.model_name,
            working_dir=inputs.original_working_dir or ctx.working_dir,
        )
        ctx.skills_info = PromptBuilder._make_skills_guide(subsystems.skill_manager)

        # Dynamic sections (below the cache boundary).
        ctx.memory, ctx.memory_context = PromptBuilder._make_memory(inputs.memory_dir)
        ctx.language = PromptBuilder._make_language(inputs.language)
        ctx.scratchpad = PromptBuilder._make_scratchpad(inputs.scratchpad_dir)
        ctx.frc, ctx.task_final_output = PromptBuilder._make_compaction_sections(config)
        ctx.pipeline_section = PromptBuilder._make_pipeline_section(subsystems.executor)

        # Per-turn ephemeral context (git / token pressure / background tasks /
        # LSP diagnostics) gathered by the turn_context bus and injected into the
        # user prompt as a <system-reminder>. None bus => "" (nothing injected).
        ctx.reminders = await PromptBuilder._make_reminders(subsystems.turn_context_bus, ctx.working_dir)

        # Protocol-specific prompt sections (command_guide) come from the active
        # channel as ONE dict — the single source, replacing the old
        # fields-on-ThinkInputs + collect_context override blocks here. The
        # channel object is already in subsystems (we also read its lower below),
        # so there is no reason to pre-extract these into the pure-data
        # ThinkInputs. None channel => the ThinkContext defaults ("").
        channel = subsystems.command_channel
        if channel is not None:
            ctx.prompt_vars = channel.prompt_vars()
            missing = set(PROMPT_VAR_KEYS) - ctx.prompt_vars.keys()
            if missing:
                raise ValueError(
                    f"command channel {type(channel).__name__}.prompt_vars() is missing "
                    f"required keys {sorted(missing)}; the templates would leak a literal "
                    f"${{...}} for each. PROMPT_VAR_KEYS = {list(PROMPT_VAR_KEYS)}."
                )

        # Capture the active channel's symbol-lowering callable so build() can
        # apply it as the final assembly step (protocol surface syntax goes in
        # last). None channel => no lowering (identity).
        ctx.lower = channel.lower if channel is not None else None
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
        """Build the compaction-gated sections: # Function Result Clearing and
        # Task Final Output Specifications.

        Emitted only when adaptive (token-based) compaction is active, since
        that is what actually clears old tool results from mote.context. Both
        describe compression artifacts: FRC warns the model that old results get
        cleared (and to write down anything it needs before that happens), and
        the final-output contract is the durable record that survives clearing.
        keep_recent comes from protected_recent_messages. Returns ("", "")
        otherwise.
        """
        rz = config.role
        if not getattr(rz, "enable_compressable_memory", False):
            return "", ""
        keep_recent = getattr(rz, "protected_recent_messages", 8)
        frc = Template(FRC_SECTION).safe_substitute(keep_recent=str(keep_recent))
        return frc, TASK_FINAL_OUTPUT_SECTION

    @staticmethod
    def _make_pipeline_section(executor) -> str:
        """Include the pipeline brief when pipeline tools or their controls exist.

        Emitted when the role has any pipeline tool (compiled-graph backed,
        listed under ``# Pipeline Tools``) or the ``resume_tasks`` / ``get_node_state``
        control commands. ``get_tool_schemas`` now excludes pipeline tools, so
        pipeline presence is read from ``get_pipeline_tool_schemas``.
        """
        schemas = executor.get_tool_schemas()
        if isinstance(schemas, dict):
            names = set(schemas.keys())
        else:
            names = {s.get("name", "") for s in schemas} if schemas else set()
        get_pipeline = getattr(executor, "get_pipeline_tool_schemas", None)
        has_pipeline = bool(get_pipeline()) if callable(get_pipeline) else False
        if not has_pipeline and not ({"resume_tasks", "get_node_state"} & names):
            return ""
        return BACKGROUND_PIPELINE_SECTION

    @staticmethod
    def _make_env_section(model_name: str, working_dir: str = "") -> str:
        cwd = working_dir or str(DEFAULT_WORKSPACE_ROOT)
        lines = [
            "# Environment",
            "You have been invoked in the following environment:",
            f" - Platform: {sys.platform}",
            f" - Shell: {os.environ.get('SHELL', '')}",
            f" - OS Version: {platform.platform()}",
            f" - You are powered by the model named {model_name}.",
            f" - Project directory: {cwd}",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _make_skills_guide(skill_manager) -> str:
        """The static Skill Loading Guide for the system prompt.

        Only the guide (constant per session) lives here; the volatile Skills
        index is delivered per-turn by SkillListingContextSource.
        """
        if skill_manager.injector:
            return skill_manager.injector.build_guide()
        return ""
