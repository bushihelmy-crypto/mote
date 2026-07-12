#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Context-engineering knobs: compaction, code map, and the Skills subsystem.

Everything that shapes *what the model sees* per turn lives here — how old tool
results get compacted, whether the code map surfaces per-symbol callers, and the
layered Skills index.
"""
from __future__ import annotations

from pydantic import Field

from mote.common.utils.yaml_model import YamlModel


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

    Gates the ``# Background Pipelines`` brief in the system prompt: when
    ``enabled`` the model is told that some tools run multi-step node graphs
    asynchronously (returning a ``task_id``, pausing for decisions). The brief is
    rendered purely off this switch — not off whether any pipeline tool happens
    to be registered — so it is an explicit, config-driven capability.
    """

    enabled: bool = Field(default=False, description="Render the # Background Pipelines system-prompt brief.")


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


class ContextConfig(YamlModel):
    """Top-level context-engineering configuration."""

    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    code_map: CodeMapConfig = Field(default_factory=CodeMapConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    bggraph: BgGraphConfig = Field(default_factory=BgGraphConfig)
