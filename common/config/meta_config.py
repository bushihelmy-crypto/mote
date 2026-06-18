#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2024/1/4 01:25
@Author  : alexanderwu
@File    : meta_config.py
"""
from pydantic import Field, model_validator

from metagpt.common.config.config.exp_pool_config import ExperiencePoolConfig
from metagpt.common.config.config.langfuse_config import LangfuseConfig
from metagpt.common.config.config.llm_config import LLMConfig
from metagpt.common.config.config.mcp_config import MCPConfig
from metagpt.common.config.config.multimodal_config import MultimodalConfig
from metagpt.common.config.config.role_zero_config import RoleZeroConfig
from metagpt.common.config.config.sentry_config import SentryConfig
from metagpt.common.utils.yaml_model import YamlModel


class Config(YamlModel):
    """Configurations for MetaGPT"""

    # Key Parameters
    llm: LLMConfig

    # Intelligent LLM routing. When True, the router picks a model per request
    # from the registered model cards (ContextProvider triggers it in the react
    # loop); when False, the configured `llm` is used as a fixed model.
    enable_router: bool = False

    # Context-compression model (autocompact summarization). Routed via the
    # "compression" task so it can differ from the main llm; transport and
    # credentials inherit from `llm` unless overridden.
    compress_llm: LLMConfig = Field(default_factory=lambda: LLMConfig(model="claude-sonnet-4-8"))

    # End-of-session summary model. Routed via the "summary" task so it can
    # differ from the main llm; transport and credentials inherit from `llm`.
    summary_llm: LLMConfig = Field(default_factory=lambda: LLMConfig(model="claude-sonnet-4-8"))

    # Global Proxy. Not used by LLM, but by other tools such as browsers.
    proxy: str = ""

    # Optional shell command that prints an API key on stdout. Used to fill
    # ``llm.api_key`` at load time only when no static/env key is present
    # (precedence: env > static config > helper). Trusted layers only — the
    # untrusted workdir layer cannot inject it (see CREDENTIAL_DENYLIST).
    api_key_helper: str = ""

    # Misc Parameters
    repair_llm_output: bool = False

    # Experience Pool Parameters
    exp_pool: ExperiencePoolConfig = Field(default_factory=ExperiencePoolConfig)

    # RoleZero's configuration
    role_zero: RoleZeroConfig = Field(default_factory=RoleZeroConfig)

    # MCP
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    # Sentry
    sentry: SentryConfig = Field(default_factory=SentryConfig)

    # Langfuse LLM observability
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)

    # Multimodal services (image/audio/music/video generation)
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)

    @model_validator(mode="after")
    def apply_task_llm_defaults(self):
        """Let the task-routed llms inherit transport/credentials from the main llm.

        By default only the model differs (claude-sonnet-4-8) on the
        compression/summary models; the endpoint, key, api type and version come
        from the configured `llm` unless the user set them explicitly.
        """
        default = LLMConfig()
        for task_llm in (self.compress_llm, self.summary_llm):
            if task_llm.base_url == default.base_url:
                task_llm.base_url = self.llm.base_url
            if task_llm.api_key == default.api_key:
                task_llm.api_key = self.llm.api_key
            if task_llm.api_type == default.api_type:
                task_llm.api_type = self.llm.api_type
            if task_llm.api_version is None:
                task_llm.api_version = self.llm.api_version
        return self

    @model_validator(mode="after")
    def activate_langfuse(self):
        """Idempotently activate Langfuse so env/client are ready before any
        LLM client is built. No-op (and no langfuse import) when disabled."""
        from metagpt.common.observability.langfuse_integration import init_langfuse

        init_langfuse(self.langfuse)
        return self

    @classmethod
    def default(cls, reload: bool = False, **kwargs) -> "Config":
        """Load the default config through the layered config center.

        Precedence (low -> high): defaults < system < user < project < workdir
        < env < cli-flags < programmatic. The user's ``metagpt/config.yaml`` is
        the trusted PROJECT layer (overriding the legacy ``config/config2.yaml``);
        ``kwargs`` are the highest-priority programmatic overrides. See
        ``common/config/loader.py``.
        """
        from metagpt.common.config.loader import load_config

        return load_config(reload=reload, programmatic=kwargs or None)
