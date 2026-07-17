#!/usr/bin/env python
# -*- coding: utf-8 -*-
from pydantic import Field, model_validator

from mote.common.config.config.context_config import ContextConfig
from mote.common.config.config.mcp_config import MCPConfig
from mote.common.config.config.models_config import ModelsConfig
from mote.common.config.config.multimodal_config import MultimodalConfig
from mote.common.config.config.observability_config import ObservabilityConfig
from mote.common.config.config.secrets_config import SecretsConfig
from mote.common.config.config.tools_config import ToolsConfig
from mote.common.config.config.ui_config import UIConfig
from mote.common.config.config.workspace_config import WorkspaceConfig
from mote.common.observability.langfuse_integration import init_langfuse
from mote.common.utils.yaml_model import YamlModel


class Config(YamlModel):
    """Configurations for Mote.

    Grouped by concern: ``models`` (which LLMs run), ``tools`` (tool runtime
    knobs), ``context`` (context engineering), ``multimodal`` (media services),
    ``mcp`` (the MCP master switch — servers live in their own
    ``mcp_config.json``, never here), ``observability`` (Sentry/Langfuse), ``ui``
    (human display), ``secrets`` (redaction/vault) and ``workspace`` (disk-layer
    TTL cleanup).
    """

    # Which models run: the default LLM, per-task overrides, routing switch and
    # the api-key helper.
    models: ModelsConfig

    # Tool-facing runtime knobs (proxy, browser locale).
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    # Context engineering (compaction, code map, skills).
    context: ContextConfig = Field(default_factory=ContextConfig)

    # Multimodal services (image/audio/music/video generation).
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)

    # MCP subsystem master switch (servers live in ``mcp_config.json``).
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    # Error tracking (Sentry) + LLM tracing (Langfuse).
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    # Human display layer (CLI) preferences — e.g. the display language. Purely
    # human-facing; model-facing text (prompts, tool output) stays English.
    ui: UIConfig = Field(default_factory=UIConfig)

    # Secret redaction / vault (opt-in). Masks known secret values in tool output
    # and vaults ``<secret>…</secret>`` uploads from the prompt before they reach
    # the model.
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    # On-disk workspace settings — currently the periodic TTL cleanup sweep that
    # reclaims stale per-session artifacts (rollout / blobs / tool_results /
    # task_outputs). Grouped here so the workspace tree has one config home.
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)

    @model_validator(mode="after")
    def activate_langfuse(self):
        """Idempotently activate Langfuse so env/client are ready before any
        LLM client is built. No-op (and no langfuse import) when disabled."""

        init_langfuse(self.observability.langfuse)
        return self
