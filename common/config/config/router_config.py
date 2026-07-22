#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Routing subsystem configuration (per-agent strategy + spawn-time routing).

Routing is a concern of its own — orthogonal to *which* models exist
(``ModelsConfig``) — so it lives in its own top-level ``router`` config block.

The strategy is chosen *per agent kind*: ``main_agent`` (root / user-facing) and
``sub_agent`` (children spawned via the Agent tool) each carry their own
:class:`AgentRouterConfig`. This lets you route only the sub-agents (cheap,
task-scoped work) while the main agent stays on a fixed model — or vice versa.

Every knob defaults to today's behavior so the block is inert until explicitly
enabled:

- ``main_agent.strategy`` / ``sub_agent.strategy`` select which
  :class:`~mote.router.strategy.RoutingStrategy` that kind of role's
  :class:`~mote.router.router.LLMRouter` is built with:
    - ``None`` (the default) — **do not route**: no strategy is installed, the
      router keeps its inert default and the role runs its fixed
      ``models.default`` (matching prior zero-behavior-change semantics).
    - ``"rule"`` — the deterministic :class:`RuleBasedStrategy` (no ML load).
    - ``"squilla"`` — the ML-backed :class:`SquillaStrategy` (Phase-3 inference
      with a graceful heuristic fallback; shared process-level engine).
  This ``strategy`` is the single routing on/off switch: ``None`` means the role
  runs its fixed ``models.default``, any concrete strategy means it routes.
  (There is no separate per-role ``enable_router`` flag any more — routing lives
  entirely in this block, per agent kind.)
- ``spawn_routing`` turns on the spawn-time *seed floor*: when a child agent is
  spawned, its first user prompt seeds an initial tier that the per-step router
  then treats as a soft floor (raise-only; step routing may still escalate
  above it, never below). Requires the child's ``sub_agent.strategy ==
  "squilla"`` (only that strategy exposes ``seed_session`` and routes) for the
  seed to be consumed.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from mote.common.utils.yaml_model import YamlModel


class AgentRouterConfig(YamlModel):
    """Per-agent-kind routing strategy selection."""

    # Which routing strategy this kind of per-role LLMRouter is constructed with.
    # None = don't route (no strategy installed, fixed models.default — status quo);
    # "rule" = deterministic RuleBasedStrategy (no ML load);
    # "squilla" = the ML-backed SquillaStrategy (shared process-level engine).
    strategy: Optional[Literal["rule", "squilla"]] = None


class RouterConfig(YamlModel):
    """Intelligent-routing knobs. Defaults reproduce today's behavior (inert)."""

    # Strategy for the root / user-facing agent.
    main_agent: AgentRouterConfig = Field(default_factory=AgentRouterConfig)

    # Strategy for children spawned via the Agent tool.
    sub_agent: AgentRouterConfig = Field(default_factory=AgentRouterConfig)

    # Spawn-time seed floor: seed a child agent's initial tier from its first
    # user prompt (a soft, raise-only floor honored by subsequent step routing).
    # No-op unless the child's ``sub_agent.strategy == "squilla"`` and the child
    # runs step routing.
    spawn_routing: bool = False
