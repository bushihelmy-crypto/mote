#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool-facing runtime knobs (network + browser fingerprint + execution policy)."""
from __future__ import annotations

from pydantic import Field

from mote.common.schema import EffectLedgerConfig, ToolResultLimitConfig, ToolSearchConfig
from mote.common.utils.yaml_model import YamlModel


class ToolsConfig(YamlModel):
    """Settings shared by tools (not by the LLM clients).

    This is the config home for the whole *tool-execution scope*: the runtime
    knobs above (proxy / browser fingerprint) plus the two cross-cutting policies
    the :class:`~mote.executor.tool_executor.ToolExecutor` owns —
    :class:`ToolResultLimitConfig` (large-result persistence) and
    :class:`EffectLedgerConfig` (EXTERNAL-effect idempotency ledger). Grouping
    both under ``tools`` keeps their single-owner discipline: the executor reads
    them from here, and the compaction spill reducer borrows ``result_limit`` off
    the built executor (never a second instance), so there is exactly one source
    for each and no drift.
    """

    # Global proxy for tools such as the browser (the LLM clients use
    # ``models.default.proxy`` instead). Keep it consistent with the
    # ``browser_locale`` exit-IP region (a zh-CN locale on a US IP is a bot tell).
    proxy: str = ""

    # Browser locale/region bundle for the WebBrowser stealth fingerprint:
    # "auto" (default) infers zh vs en from the host env; "en" / "zh" force a
    # coherent locale + timezone + Accept-Language. A per-role
    # ``role_schema.browser_locale`` (when not "auto") overrides this.
    browser_locale: str = "auto"

    # Large-tool-result policy: cap a single result and persist the overflow to
    # a session file, leaving an inline ``<persisted-output>`` preview + pointer.
    # Also drives semantic output compression (git/pytest/ruff). Defaults
    # reproduce the out-of-the-box behavior; the compaction spill reducer reuses
    # this same instance (borrowed off the executor) so history-level spill and
    # tool-exec limiting stay in lockstep.
    result_limit: ToolResultLimitConfig = Field(default_factory=ToolResultLimitConfig)

    # EXTERNAL-effect idempotency ledger (crash-replay guard). Records a durable
    # started/completed/failed entry per EXTERNAL ``(session, tool_call_id)`` so a
    # resume after a mid-call crash heals a dangling call from the recorded result
    # instead of re-running its side effect. ``enabled=False`` reproduces the
    # prior no-ledger behavior (every call simply runs).
    effect_ledger: EffectLedgerConfig = Field(default_factory=EffectLedgerConfig)

    # Tool Search master switch (deferred-tool discovery). Per-role
    # ``RoleSchema.deferred_tools`` declares WHICH tools are hidden-until-searched;
    # this ``enabled`` flag is the global OVERRIDE gating whether that declaration
    # takes effect at all. ``enabled=False`` forces the effective deferred set to
    # EMPTY for every role — SearchTools is not bound, the compact menu is not
    # built, and no native tool-search path fires; every declared tool is simply
    # fully visible (the plain no-deferral path).
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig)
