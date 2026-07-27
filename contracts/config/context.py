#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Context-engineering knobs: compaction, code map, and the Skills subsystem.

Everything that shapes *what the model sees* per turn lives here — how old tool
results get compacted, whether the code map surfaces per-symbol callers, and the
layered Skills index.
"""
from __future__ import annotations

from pydantic import Field

from mote.contracts.config.base import ConfigModel as YamlModel


class CompactionConfig(YamlModel):
    """Adaptive (token-based) compaction of the working context.

    When ``enabled`` the think-engine emits the compaction-aware prompt sections
    (Function Result Clearing / summarize / task-final-output) that tell the
    model old tool results get cleared; the actual clearing is run by
    ``context.compaction`` (ContextManager). ``protected_recent_messages`` is how
    many recent messages the FRC section says are kept intact.
    """

    enabled: bool = Field(default=False, description="Use adaptive compaction memory.")
    protected_recent_messages: int = Field(
        default=8, description="Number of recent messages to protect from compression."
    )


class CodeMapConfig(YamlModel):
    """Code-map (repository symbol overview) knobs."""

    # Opportunistically surface real per-symbol callers of calm public symbols
    # (``foo called by: a.py``) via the LSP references facade, not only when an
    # interface breaks. Default off — it adds LSP ``references`` volume, so it is
    # opt-in; no effect unless an LSP layer is configured.
    surface_callers: bool = Field(
        default=False, description="Surface per-symbol callers of calm public symbols in the code map (needs LSP)."
    )


class BgGraphConfig(YamlModel):
    """Background node-graph (pipeline) engine knobs.

    Gates pipeline tool *loading* at construction: when ``enabled`` the pipeline
    tools are bound and available; when off they are never bound (see
    ``ToolExecutor`` / ``TestPipelinesEnabledGate``). An explicit, config-driven
    capability switch — not a function of whether any pipeline tool happens to be
    registered.
    """

    enabled: bool = Field(default=False, description="Enable the background node-graph (pipeline) engine.")


class SkillsConfig(YamlModel):
    """P0 Skills subsystem configuration."""

    enabled: bool = Field(default=False, description="P0 Skills master switch.")
    max_tokens: int = Field(default=2000, description="Token limit for the Skills index injection.")
    # Layered source directories (precedence-as-data: bundled < user < project
    # < extra). The user toggle adds ``~/.mote/skills``; the project toggle adds
    # every ``<dir>/.mote/skills`` found walking from cwd up to the git root.
    # ``extra_dirs`` appends arbitrary highest-priority
    # directories. Same-name skills in a higher layer override lower ones.
    include_user_dir: bool = Field(default=True, description="Scan ~/.mote/skills for user-level skills.")
    include_project_dir: bool = Field(
        default=True, description="Scan <dir>/.mote/skills (cwd→git-root walk) for project-level skills."
    )
    extra_dirs: list[str] = Field(
        default_factory=list, description="Additional (highest-priority) skill source directories."
    )


class TurnContextConfig(YamlModel):
    """Which per-turn *ephemeral* context sources are active — an opt-out registry.

    Every normally-on ephemeral source (git status, token / fold pressure, the
    code map, the deferred-tool menu, timestamp, ...) is active by default; list
    a source's ``name`` in ``disabled`` to suppress it (opt-out / blacklist). The
    filter matches the source's stable ``name`` attribute, so e.g.
    ``disabled: ["git", "code_map"]`` drops those two blocks from the reminder
    envelope while leaving every other feed untouched.

    The credential index is the one **opt-in exception**: it presents a login-fill
    menu to the model, so it stays off until ``credential_index: true`` — and even
    then only renders on a turn where WebBrowser was recently used (its consumer).
    Two orthogonal knobs shape WHAT it lists:

    - ``credential_keys`` — a whitelist of which configured *secret* names to
      expose (as ``<agent-vault:key>`` placeholders, never values). Empty (the
      default) exposes every configured secret; a non-empty list narrows it to
      exactly those keys.
    - ``credential_values`` — inline **non-secret** ``name: value`` pairs (e.g. a
      username / display name). These are NOT stored in the vault and are rendered
      as their literal value for the model to type directly — use only for
      non-sensitive fields you are comfortable placing in plaintext config.
    """

    disabled: list[str] = Field(
        default_factory=list,
        description="Names of ephemeral turn-context sources to suppress (opt-out blacklist).",
    )
    credential_index: bool = Field(
        default=False,
        description=(
            "Master toggle for the login-credential menu (secret NAMES + inline "
            "non-secret values). Opt-in; only renders when WebBrowser was recently used."
        ),
    )
    credential_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Whitelist of configured secret names to expose as login placeholders. "
            "Empty = expose all configured secrets."
        ),
    )
    credential_values: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Inline NON-secret name:value pairs (e.g. username) shown as literal "
            "values for the model to type directly. Not stored in the vault."
        ),
    )


class ContextConfig(YamlModel):
    """Top-level context-engineering configuration."""

    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    code_map: CodeMapConfig = Field(default_factory=CodeMapConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    bggraph: BgGraphConfig = Field(default_factory=BgGraphConfig)
    turn_context: TurnContextConfig = Field(default_factory=TurnContextConfig)
