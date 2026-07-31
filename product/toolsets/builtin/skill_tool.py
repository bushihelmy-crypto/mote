"""Skill tool — the single bridge for invoking project Skills on-demand.

Skills are listed (by name + description) in the system-prompt "Available
Skills" index; their full instructions stay on disk until invoked. This tool is
that invocation entry point:

* ``Skill(name=..., arguments=...)`` loads a skill. An *inline* skill returns
  its rendered instructions as the tool result (the model reads them in the
  main conversation). A *fork* skill runs its instructions inside a fresh,
  isolated sub-agent (tools limited to the skill's ``allowed-tools``) and
  returns only that sub-agent's final summary — so the long process never
  pollutes the main history.
* ``Skill(query=...)`` searches *all* discovered skills (including long-tail
  ones not shown in the steady index) and returns the matching index rows.

This keeps the steady context cost flat as the skill count grows: only index
rows live in the prompt, never the full bodies.
"""

from __future__ import annotations

from typing import ClassVar

from mote.runtime.errors import ToolError
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import GetCwd, GetSkillPool, RegisterResource, RunSkillFork

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise/return site).
_MSG_NO_SKILLS = "No skills are available in this session."
_MSG_NAME_OR_QUERY_REQUIRED = 'Provide a skill "name" to invoke, or a "query" to search for one.'
_MSG_UNKNOWN_SKILL = "Unknown skill '{name}'. Available: {available}"
_MSG_HUMAN_ONLY = "Skill '{name}' is human-invocable only."
_MSG_NO_MATCH = "No skills match '{query}'."


class Skill(BaseTool):
    """Invoke a project Skill by name, or search for one by keyword.

    Use the ``Available Skills`` index in the system prompt to pick a skill,
    then call this tool with that skill's ``name``. Pass any task input in
    ``arguments``. To find skills not shown in the index, pass ``query`` with
    keywords instead of ``name``.
    """

    name = "Skill"
    # Recall synonyms for tool-search: ways a model asks for a packaged
    # procedure that the summary ("invoke a project Skill") does not spell out.
    keywords: ClassVar[list[str]] = [
        "procedure",
        "playbook",
        "recipe",
        "macro",
        "capability",
        "invoke skill",
        "技能",
        "调用技能",
        "流程",
    ]
    # ``get_cwd`` lets fork skills inherit the working dir; ``get_skill_pool``
    # resolves the live skill pool; ``run_skill_fork`` spawns the isolated child;
    # ``register_resource`` registers an inline body so it survives compaction.
    requires = ("get_cwd", "get_skill_pool", "run_skill_fork", "register_resource")

    # Injected from Role by bind(). ``register_resource`` defaults to a no-op stub
    # so the tool keeps working when bound without a Role (standalone / tests) —
    # inline-body re-projection is best-effort bookkeeping, not core behavior.
    get_cwd: GetCwd
    get_skill_pool: GetSkillPool
    run_skill_fork: RunSkillFork
    register_resource: RegisterResource = staticmethod(lambda **k: None)

    async def call(self, *, name: str = "", arguments: str = "", query: str = "") -> str:
        """Invoke or search reusable skills — packaged multi-step procedures.

        Invoke a skill by ``name`` (as listed in the Available Skills index), or
        search skills with ``query`` when the name is unknown. An inline skill
        returns its rendered instructions as the result; a fork skill runs in an
        isolated sub-agent and returns only its final summary.

        Args:
            name: The skill to invoke (as listed in the Available Skills index).
            arguments: Task input passed to the skill (substituted for
                ``$ARGUMENTS`` in inline skills; the prompt for fork skills).
            query: Keywords to search skills by, when ``name`` is unknown.
                Returns matching index rows instead of running a skill.
        """
        pool = self.get_skill_pool()
        if pool is None or pool.get_skill_count() == 0:
            raise ToolError(_MSG_NO_SKILLS)

        # Search mode: query without a concrete name.
        if query and not name:
            return self._search(pool, query)

        name = (name or "").strip()
        if not name:
            raise ToolError(_MSG_NAME_OR_QUERY_REQUIRED)

        skill = pool.get(name)
        if skill is None:
            available = ", ".join(sorted(s.name for s in pool.get_all())) or "(none)"
            raise ToolError(_MSG_UNKNOWN_SKILL.format(name=name, available=available))
        if skill.disable_model_invocation:
            raise ToolError(_MSG_HUMAN_ONLY.format(name=name))

        rendered = self._render(skill, arguments)

        if skill.context == "fork":
            summary = await self.run_skill_fork(
                instructions=rendered,
                arguments=arguments,
                allowed_tools=list(skill.allowed_tools),
                model=skill.model,
                effort=skill.effort,
            )
            return summary or "Skill finished without a summary."

        # Inline: the rendered body becomes the tool result. Register it as a
        # sticky resource so it is re-projected after the head is compacted away
        # (the tool result itself is a Skill message, which microcompact never
        # folds, but autocompact can still discard it with the head).
        self._register_resource(name, rendered)
        return rendered

    def _register_resource(self, name: str, rendered: str) -> None:
        """Register a loaded inline skill body for post-compaction re-projection.

        Best-effort and non-throwing: the ``register_resource`` capability
        defaults to a no-op stub when unbound (no Role), so the tool keeps
        working standalone and in tests.
        """
        try:
            self.register_resource(id=name, kind="skill", content=rendered)
        except Exception:  # never let bookkeeping break the tool result
            pass

    def _render(self, skill, arguments: str) -> str:
        """Substitute the supported placeholders in the skill body.

        Replaces ``$ARGUMENTS`` / ``${SKILL_DIR}`` / ``${SESSION_ID}``. Plain
        ``.replace`` (not ``string.Template``) so arbitrary ``$`` in the body
        (shell, code) is never mangled. Shell-style ``!`...``` expansion is
        intentionally NOT supported in v1 (deferred for safety).
        """
        body = skill.instructions or ""
        skill_dir = str(skill.source_path.parent) if skill.source_path else ""
        body = body.replace("$ARGUMENTS", arguments or "")
        body = body.replace("${SKILL_DIR}", skill_dir)
        body = body.replace("${SESSION_ID}", self.session_id)
        return body

    @staticmethod
    def _search(pool, query: str) -> str:
        """Return index rows for skills matching ``query`` (substring, case-insensitive).

        Matches against name + description + when_to_use across every discovered
        skill (the long tail not shown in the steady index too).
        """
        q = query.strip().lower()
        matches = []
        for s in pool.get_all():
            if s.disable_model_invocation:
                continue
            haystack = f"{s.name} {s.description} {s.when_to_use}".lower()
            if q in haystack:
                matches.append(s)

        if not matches:
            return _MSG_NO_MATCH.format(query=query)

        lines = [f"Skills matching '{query}':", ""]
        for s in matches:
            desc = s.description or ""
            if s.when_to_use:
                desc = f"{desc} (use when: {s.when_to_use})" if desc else s.when_to_use
            row = f"- {s.name}: {desc}"
            if s.argument_hint:
                row += f" [args: {s.argument_hint}]"
            lines.append(row)
        return "\n".join(lines)
