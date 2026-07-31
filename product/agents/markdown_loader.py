"""Markdown-defined agents — ``.mote/agents/*.md`` → Role type definitions.

A project (or the user) can declare a spawnable subagent by
dropping a Markdown file with YAML frontmatter under ``.mote/agents/`` (walked
from cwd up to the git root) or ``~/.mote/agents/``. Each file becomes a
``(BaseAgent, Role)`` subclass. Product assembly freezes those definitions into
an Application-owned catalog, so the ``Agent`` tool can spawn it exactly like a
hand-written Python agent without consulting process-global state.

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
project files override same-named ``~/.mote/agents`` files before Product
assembly creates the immutable catalog snapshot.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Optional

from mote.contracts.agent import BaseAgent
from mote.contracts.model.topology import SemanticRoute
from mote.contracts.model.topology_codec import decode_route_id
from mote.product.paths import mote_project_dirs, user_mote_dir
from mote.product.skills.markdown import MarkdownMetaParser
from mote.runtime.agent.role import Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.role_state import RoleState
from mote.runtime.telemetry.logging import logger

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


def _build_agent_class(name: str, meta: dict, body: str) -> Optional[type[BaseAgent]]:
    """Construct a ``(BaseAgent, Role)`` subclass from parsed frontmatter + body.

    Returns None when the definition is invalid (missing name/description). The
    The generated class is a concrete Role assembled from a product declaration.
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

    class _MarkdownAgent(BaseAgent, Role):
        role_type_id = f"mote.agent.markdown.{name}.v1"
        replace_role_type_registration = True
        agent_name = name
        # class-level so the Agent tool's ``custom_schema`` listing can read it
        # without instantiating (getattr(agent_cls, "tools")).
        tools = list(tool_list) if tool_list else []

        def __init__(
            self,
            *,
            parent_session_id: Optional[str] = None,
            wiring=None,
            config=None,
            **_ignored,
        ):
            schema_kwargs: dict = {
                "name": name,
                "instruction": instruction,
                "desc": description,
            }
            if tool_list is not None:
                schema_kwargs["tools"] = list(tool_list)
            if model:
                schema_kwargs["model_route"] = (
                    decode_route_id(model)
                    if model == "default" or model.startswith(("task:", "semantic:"))
                    else SemanticRoute(name=model)
                )
            schema = RoleSchema(**schema_kwargs)
            state = RoleState(parent_session_id=parent_session_id)
            Role.__init__(
                self,
                role_schema=schema,
                state=state,
                wiring=wiring,
                config=config,
            )

    _MarkdownAgent.aliases = list(aliases)
    _MarkdownAgent.description = description
    identity = json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str) + "\n" + instruction
    _MarkdownAgent.definition_version = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    _MarkdownAgent.__name__ = f"MarkdownAgent_{name}"
    _MarkdownAgent.__qualname__ = _MarkdownAgent.__name__
    return _MarkdownAgent


def discover_md_agents(cwd: Optional[Path] = None) -> dict[str, type[BaseAgent]]:
    """Discover ``.mote/agents/*.md`` agents, low→high precedence (closer wins).

    Scans ``~/.mote/agents`` then the ``<dir>/.mote/agents`` git-root→cwd walk;
    a later (higher-precedence) file of the same agent name overrides an earlier
    one. Returns a ``{name: agent_class}`` map (unregistered). Best-effort: bad
    files are skipped, never raised.
    """
    parser = MarkdownMetaParser()
    dirs: List[Path] = [
        user_mote_dir(_AGENTS_SUBDIR),
        *mote_project_dirs(_AGENTS_SUBDIR, cwd),
    ]

    found: dict[str, type[BaseAgent]] = {}
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


__all__ = ["discover_md_agents"]
