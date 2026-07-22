"""Markdown-defined agents — ``.mote/agents/*.md`` → registered Role types.

A project (or the user) can declare a spawnable subagent by
dropping a Markdown file with YAML frontmatter under ``.mote/agents/`` (walked
from cwd up to the git root) or ``~/.mote/agents/``. Each file becomes a
``(BaseAgent, Role)`` subclass registered in the :mod:`agent_registry`, so the
``Agent`` tool can spawn it exactly like a hand-written Python agent.

Frontmatter (all optional except ``name`` + ``description``)::

    ---
    name: reviewer                 # agent type / lookup key (required)
    description: Reviews a diff…    # the "when to use" surfaced to the LLM (required)
    tools: [Read, Search]          # tool allowlist (str CSV or list; empty/'*'/absent = all)
    model: claude-sonnet-4-6       # optional per-agent model override
    aliases: [rev, code-reviewer]  # optional extra lookup names
    ---
    <markdown body = the agent's system-prompt instruction>

Discovery is best-effort: a malformed / nameless / descriptionless file is
skipped (logged), never fatal. A closer-to-cwd file overrides a farther one, and
project files override same-named ``~/.mote/agents`` files (registration is
idempotent for the same class; a *different* class under a taken name is a
registry conflict, so we register lowest-priority first and let a later layer
replace the entry before registration by keying on the discovery map).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from mote.common.base.agent import BaseAgent
from mote.common.const.paths import mote_project_dirs, user_mote_dir
from mote.common.logs import logger
from mote.common.utils.markdown_meta_parser import MarkdownMetaParser

_AGENTS_SUBDIR = "agents"


def _normalize_tools(raw) -> Optional[List[str]]:
    """Turn a frontmatter ``tools`` value into a tool-name list, or None for 'all'.

    Accepts a comma-separated string or a list. ``'*'`` / empty / absent means
    "inherit the full toolbox" (None).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw or raw == "*":
            return None
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        names = [str(t).strip() for t in raw if str(t).strip()]
        return names or None
    return None


def _build_agent_class(name: str, meta: dict, body: str) -> Optional[type]:
    """Construct a ``(BaseAgent, Role)`` subclass from parsed frontmatter + body.

    Returns None when the definition is invalid (missing name/description). The
    The implementation lives in ``roles`` because the generated class is a
    concrete Role; only registration is delegated downward to executor.
    """
    description = str(meta.get("description", "")).strip()
    if not name or not description:
        logger.debug(f"md-agent: skipping '{name}' — missing name or description")
        return None

    tool_list = _normalize_tools(meta.get("tools"))
    model = str(meta.get("model", "") or "").strip()
    aliases_raw = meta.get("aliases")
    aliases = _normalize_tools(aliases_raw) or []
    instruction = body.strip()

    from mote.roles import Role
    from mote.roles.role_schema import RoleSchema
    from mote.roles.role_state import RoleState

    class _MarkdownAgent(BaseAgent, Role):
        agent_name = name
        # class-level so the Agent tool's ``custom_schema`` listing can read it
        # without instantiating (getattr(agent_cls, "tools")).
        tools = list(tool_list) if tool_list else []

        def __init__(self, *, parent_session_id: Optional[str] = None, context=None, config=None, **_ignored):
            schema_kwargs: dict = {
                "name": name,
                "instruction": instruction,
                "desc": description,
            }
            if tool_list is not None:
                schema_kwargs["tools"] = list(tool_list)
            schema = RoleSchema(**schema_kwargs)
            state = RoleState(parent_session_id=parent_session_id)
            child_config = config
            if model and child_config is not None:
                try:
                    child_config = child_config.model_copy(deep=True)
                    child_config.models.default.model = model
                except Exception:  # noqa: BLE001 — model override is best-effort
                    child_config = config
            Role.__init__(self, role_schema=schema, state=state, context=context, config=child_config)

    _MarkdownAgent.aliases = list(aliases)
    _MarkdownAgent.description = description
    _MarkdownAgent.__name__ = f"MarkdownAgent_{name}"
    _MarkdownAgent.__qualname__ = _MarkdownAgent.__name__
    return _MarkdownAgent


def discover_md_agents(cwd: Optional[Path] = None) -> dict[str, type]:
    """Discover ``.mote/agents/*.md`` agents, low→high precedence (closer wins).

    Scans ``~/.mote/agents`` then the ``<dir>/.mote/agents`` git-root→cwd walk;
    a later (higher-precedence) file of the same agent name overrides an earlier
    one. Returns a ``{name: agent_class}`` map (unregistered). Best-effort: bad
    files are skipped, never raised.
    """
    parser = MarkdownMetaParser()
    dirs: List[Path] = [user_mote_dir(_AGENTS_SUBDIR), *mote_project_dirs(_AGENTS_SUBDIR, cwd)]

    found: dict[str, type] = {}
    seen_roots: set[str] = set()
    for root in dirs:
        if not root.is_dir():
            continue
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        for md in sorted(root.glob("*.md")):
            try:
                doc = parser.parse(md)
            except Exception as exc:  # noqa: BLE001 — a bad file is skipped, not fatal
                logger.warning(f"md-agent: failed to parse {md}: {exc}")
                continue
            meta = doc.metadata or {}
            name = str(meta.get("name", "") or md.stem).strip()
            agent_cls = _build_agent_class(name, meta, doc.content)
            if agent_cls is not None:
                found[name] = agent_cls  # higher-precedence dir overrides
    return found


def register_md_agents(cwd: Optional[Path] = None) -> List[str]:
    """Discover and register ``.mote/agents/*.md`` agents into the agent registry.

    Returns the list of registered agent names. Idempotent per name: a name
    already taken by a *different* class is left as-is (logged), so a hand-written
    Python agent always wins over a same-named markdown file.
    """
    # Local import to avoid a module-load cycle (registry ← agent tool ← …).
    from mote.executor.agent_registry import registry

    registered: List[str] = []
    for name, agent_cls in discover_md_agents(cwd).items():
        existing = registry.get(name)
        if existing is not None and existing is not agent_cls:
            if not getattr(existing, "__name__", "").startswith("MarkdownAgent_"):
                # A hand-written Python agent owns this name — it always wins.
                logger.debug(f"md-agent: '{name}' already registered to a Python agent; skipping")
                continue
            # Re-scan replacing a prior markdown agent: purge every key (primary
            # + aliases) that pointed at the stale class, then fall through to a
            # clean re-register so aliases can't dangle at the old object.
            stale = existing
            for key in [k for k, v in registry._registry.items() if v is stale]:  # noqa: SLF001
                del registry._registry[key]  # noqa: SLF001 — controlled reload
        try:
            registry.register(agent_cls)
            registered.append(name)
        except (TypeError, ValueError) as exc:
            logger.warning(f"md-agent: could not register '{name}': {exc}")
    return registered


__all__ = ["discover_md_agents", "register_md_agents"]
