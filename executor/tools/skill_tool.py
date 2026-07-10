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

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError


@register_tool
class Skill(BaseTool):
    """Invoke a project Skill by name, or search for one by keyword.

    Use the ``Available Skills`` index in the system prompt to pick a skill,
    then call this tool with that skill's ``name``. Pass any task input in
    ``arguments``. To find skills not shown in the index, pass ``query`` with
    keywords instead of ``name``.
    """

    name = "Skill"
    # ``get_cwd`` lets fork skills inherit the working dir; ``get_skill_pool``
    # resolves the live skill pool; ``run_skill_fork`` spawns the isolated child;
    # ``register_resource`` registers an inline body so it survives compaction.
    requires = ("get_cwd", "get_skill_pool", "run_skill_fork", "register_resource")

    async def call(self, *, name: str = "", arguments: str = "", query: str = "") -> str:
        """Invoke a skill by ``name``, or search skills with ``query``.

        Args:
            name: The skill to invoke (as listed in the Available Skills index).
            arguments: Task input passed to the skill (substituted for
                ``$ARGUMENTS`` in inline skills; the prompt for fork skills).
            query: Keywords to search skills by, when ``name`` is unknown.
                Returns matching index rows instead of running a skill.
        """
        pool = self.get_skill_pool()  # type: ignore[attr-defined]
        if pool is None or pool.get_skill_count() == 0:
            raise ToolError("No skills are available in this session.")

        # Search mode: query without a concrete name.
        if query and not name:
            return self._search(pool, query)

        name = (name or "").strip()
        if not name:
            raise ToolError(
                'Provide a skill "name" to invoke, or a "query" to search for one.'
            )

        skill = pool.get(name)
        if skill is None:
            available = ", ".join(sorted(s.name for s in pool.get_all())) or "(none)"
            raise ToolError(f"Unknown skill '{name}'. Available: {available}")
        if skill.disable_model_invocation:
            raise ToolError(f"Skill '{name}' is human-invocable only.")

        rendered = self._render(skill, arguments)

        if skill.context == "fork":
            summary = await self.run_skill_fork(  # type: ignore[attr-defined]
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

        Best-effort and non-throwing: no-op when unbound (no Role injected the
        ``register_resource`` capability), so the tool keeps working standalone
        and in tests.
        """
        register = getattr(self, "register_resource", None)
        if register is None:
            return
        try:
            register(id=name, kind="skill", content=rendered)
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
            return f"No skills match '{query}'."

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
