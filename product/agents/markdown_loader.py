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
from mote.product.extensions.sources import ExtensionKind, ExtensionSource, ExtensionSourcePolicy
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


def _build_agent_class(
    name: str,
    meta: dict,
    body: str,
    *,
    source: ExtensionSource,
) -> Optional[type[BaseAgent]]:
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
    approved_schema = RoleSchema(**schema_kwargs)
    identity_payload = {
        "schema": "mote.markdown-agent-definition/v1",
        "source": {
            "scope": source.scope.value,
            "canonical_path": str(source.canonical_path),
            "device": source.device,
            "inode": source.inode,
            "content_digest": source.content_digest,
            "approval_principal": source.approval_principal,
        },
    }
    identity = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    definition_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    definition_id = f"mote.agent.markdown.v1.sha256-{definition_digest}"

    class _MarkdownAgent(BaseAgent, Role):
        # Markdown definitions are Application-scoped declarations, never
        # process-global polymorphic registrations.
        role_type_id = None
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
            role_schema: RoleSchema | None = None,
            state: RoleState | None = None,
        ):
            schema = approved_schema.model_copy(deep=True)
            if role_schema is not None and role_schema != schema:
                raise ValueError("Markdown Agent snapshot definition does not match approved source")
            restored_state = state or RoleState(parent_session_id=parent_session_id)
            if state is not None and parent_session_id is not None and state.parent_session_id != parent_session_id:
                raise ValueError("Markdown Agent parent identity conflicts with restored state")
            Role.__init__(
                self,
                role_schema=schema,
                state=restored_state,
                wiring=wiring,
                config=config,
            )

        @property
        def residency_definition_id(self) -> str:
            return definition_id

    _MarkdownAgent.aliases = list(aliases)
    _MarkdownAgent.description = description
    # Spawn catalog and Session/Residency carry the exact same opaque identity.
    _MarkdownAgent.definition_version = definition_id
    _MarkdownAgent.definition_id = definition_id
    _MarkdownAgent.definition_source_path = str(source.canonical_path)
    _MarkdownAgent.definition_source_digest = source.content_digest
    _MarkdownAgent.__name__ = f"MarkdownAgent_{name}"
    _MarkdownAgent.__qualname__ = _MarkdownAgent.__name__
    return _MarkdownAgent


def discover_md_agents(
    cwd: Optional[Path],
    *,
    source_policy: ExtensionSourcePolicy,
) -> dict[str, type[BaseAgent]]:
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
    seen_sources: set[tuple[int, int]] = set()
    for root in dirs:
        if not root.is_dir():
            continue
        sources = source_policy.admitted_files(ExtensionKind.AGENT, sorted(root.glob("*.md")))
        for source in sources:
            identity = (source.device, source.inode)
            if identity in seen_sources:
                continue
            seen_sources.add(identity)
            md = source.canonical_path
            doc = parser.parse_text(source.content.decode("utf-8"), source_path=md)
            meta = doc.metadata or {}
            name = str(meta.get("name", "") or md.stem).strip()
            agent_cls = _build_agent_class(name, meta, doc.content, source=source)
            if agent_cls is not None:
                found[name] = agent_cls  # higher-precedence dir overrides
    return found


__all__ = ["discover_md_agents"]
