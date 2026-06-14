#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2024/1/4 01:25
@Author  : alexanderwu
@File    : config2.py
"""
import os
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from metagpt.common.config.backend_config import BackendConfig, BackendType
from metagpt.common.config.browser_config import BrowserConfig
from metagpt.common.config.dr_config import DrLeaderConfig, DrSearchConfig, DrWriterConfig
from metagpt.common.config.embedding_config import EmbeddingConfig
from metagpt.common.config.exp_pool_config import ExperiencePoolConfig
from metagpt.common.config.frontend_engineer_config import FrontendEngineerConfig
from metagpt.common.config.funcsea_config import FuncseaConfig
from metagpt.common.config.image_search_config import ImageSearchConfig
from metagpt.common.config.langfuse_config import LangfuseConfig
from metagpt.common.config.llm_config import LLMConfig, LLMType
from metagpt.common.config.mcp_config import MCPConfig
from metagpt.common.config.mermaid_config import MermaidConfig
from metagpt.common.config.multimodal_config import MultimodalConfig
from metagpt.common.config.omniparse_config import OmniParseConfig
from metagpt.common.config.redis_config import RedisConfig
from metagpt.common.config.role_custom_config import RoleCustomConfig
from metagpt.common.config.role_zero_config import RoleZeroConfig
from metagpt.common.config.s3_config import S3Config
from metagpt.common.config.search_config import SearchConfig
from metagpt.common.config.sentry_config import SentryConfig
from metagpt.common.config.service_to_service_jwt_config import ServiceToServiceJWTConfig
from metagpt.common.config.supabase_config import SupabaseConfig
from metagpt.common.config.useragent_config import UseragentConfig
from metagpt.common.config.workspace_config import WorkspaceConfig
from metagpt.common.const import CONFIG_ROOT, METAGPT_ROOT
from metagpt.common.utils.yaml_model import YamlModel


class CLIParams(BaseModel):
    """CLI parameters"""

    project_path: str = ""
    project_name: str = ""
    inc: bool = False
    reqa_file: str = ""
    max_auto_summarize_code: int = 0
    git_reinit: bool = False

    @model_validator(mode="after")
    def check_project_path(self):
        """Check project_path and project_name"""
        if self.project_path:
            self.inc = True
            self.project_name = self.project_name or Path(self.project_path).name
        return self


class Config(CLIParams, YamlModel):
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

    # RAG Embedding
    embedding: EmbeddingConfig = EmbeddingConfig()

    # Global Proxy. Not used by LLM, but by other tools such as browsers.
    proxy: str = ""

    # Tool Parameters
    search: SearchConfig = SearchConfig()
    browser: BrowserConfig = BrowserConfig()
    mermaid: MermaidConfig = MermaidConfig()
    dr_search: DrSearchConfig = DrSearchConfig()
    dr_leader: DrLeaderConfig = DrLeaderConfig()
    dr_writer: DrWriterConfig = DrWriterConfig()
    dr_search_checklist_gen_llm: LLMConfig = LLMConfig()
    dr_search_compress_llm: LLMConfig = LLMConfig()

    # SEO Specialist writer LLM
    seo_write_llm: LLMConfig = LLMConfig()

    # Storage Parameters
    s3: Optional[S3Config] = None
    redis: Optional[RedisConfig] = None

    # Misc Parameters
    repair_llm_output: bool = False
    prompt_schema: Literal["json", "markdown", "raw"] = "json"
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    enable_longterm_memory: bool = False
    code_validate_k_times: int = 2

    # Experience Pool Parameters
    exp_pool: ExperiencePoolConfig = Field(default_factory=ExperiencePoolConfig)

    # Will be removed in the future
    metagpt_tti_url: str = ""
    language: str = "English"
    redis_key: str = "placeholder"
    iflytek_app_id: str = ""
    iflytek_api_secret: str = ""
    iflytek_api_key: str = ""
    azure_tts_subscription_key: str = ""
    azure_tts_region: str = ""

    # Role's custom configuration
    roles: Optional[List[RoleCustomConfig]] = None

    # RoleZero's configuration
    role_zero: RoleZeroConfig = Field(default_factory=RoleZeroConfig)
    frontend_engineer: FrontendEngineerConfig = Field(default_factory=FrontendEngineerConfig)

    omniparse: Optional[OmniParseConfig] = None

    # Supabase
    supabase: SupabaseConfig = Field(default_factory=SupabaseConfig)

    # Useragent
    useragent: UseragentConfig = Field(default_factory=UseragentConfig)

    # Config for the unsplash api key
    image_search: ImageSearchConfig = Field(default_factory=ImageSearchConfig)

    # Unified multimodal capabilities config
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)

    # MCP
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    # Sentry
    sentry: SentryConfig = Field(default_factory=SentryConfig)

    # Langfuse LLM observability
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)

    # FuncSea
    funcsea: FuncseaConfig = Field(default_factory=FuncseaConfig)

    # Backend
    backend: BackendConfig = Field(default_factory=BackendConfig)

    # Service-to-service JWT
    s2s_jwt: ServiceToServiceJWTConfig = Field(default_factory=ServiceToServiceJWTConfig)

    @model_validator(mode="after")
    def apply_multimodal_ai_defaults(self):
        if not self.multimodal.ai_service.base_url:
            self.multimodal.ai_service.base_url = self.llm.base_url
        if not self.multimodal.ai_service.api_key:
            llm_key = self.llm.api_key
            # llm.api_key may be a list (multi-key rotation); the multimodal field is a str.
            self.multimodal.ai_service.api_key = llm_key[0] if isinstance(llm_key, list) else llm_key
        self.multimodal.inherit_ai_service_defaults()
        return self

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

    @model_validator(mode="after")
    def valid_backend(self):
        if self.backend.backend_type == BackendType.FUNCSEA:
            self.backend.config = self.funcsea
        elif self.backend.backend_type == BackendType.SUPABASE:
            self.backend.config = self.supabase
        return self

    @classmethod
    def from_home(cls, path):
        """Load config from ~/.metagpt/config2.yaml"""
        pathname = CONFIG_ROOT / path
        if not pathname.exists():
            return None
        return Config.from_yaml_file(pathname)

    @classmethod
    def default(cls, reload: bool = False, **kwargs) -> "Config":
        """Load default config
        - Priority: env < default_config_paths
        - Inside default_config_paths, the latter one overwrites the former one
        """
        default_config_paths = (
            METAGPT_ROOT / "config/config2.yaml",
            CONFIG_ROOT / "config2.yaml",
        )
        if reload or default_config_paths not in _CONFIG_CACHE:
            dicts = [dict(os.environ), *(Config.read_yaml(path) for path in default_config_paths), kwargs]
            final = merge_dict(dicts)
            _CONFIG_CACHE[default_config_paths] = Config(**final)
        return _CONFIG_CACHE[default_config_paths]

    @classmethod
    def from_llm_config(cls, llm_config: dict):
        """user config llm
        example:
        llm_config = {"api_type": "xxx", "api_key": "xxx", "model": "xxx"}
        gpt4 = Config.from_llm_config(llm_config)
        A = Role(name="A", profile="Democratic candidate", goal="Win the election", actions=[a1], watch=[a2], config=gpt4)
        """
        llm_config = LLMConfig.model_validate(llm_config)
        dicts = [dict(os.environ)]
        dicts += [{"llm": llm_config}]
        final = merge_dict(dicts)
        return Config(**final)

    def update_via_cli(self, project_path, project_name, inc, reqa_file, max_auto_summarize_code):
        """update config via cli"""

        # Use in the PrepareDocuments action according to Section 2.2.3.5.1 of RFC 135.
        if project_path:
            inc = True
            project_name = project_name or Path(project_path).name
        self.project_path = project_path
        self.project_name = project_name
        self.inc = inc
        self.reqa_file = reqa_file
        self.max_auto_summarize_code = max_auto_summarize_code

    def get_openai_llm(self) -> Optional[LLMConfig]:
        """Get OpenAI LLMConfig by name. If no OpenAI, raise Exception"""
        if self.llm.api_type == LLMType.OPENAI:
            return self.llm
        return None

    def get_azure_llm(self) -> Optional[LLMConfig]:
        """Get Azure LLMConfig by name. If no Azure, raise Exception"""
        if self.llm.api_type == LLMType.AZURE:
            return self.llm
        return None


def merge_dict(dicts: Iterable[Dict]) -> Dict:
    """Merge multiple dicts into one, with the latter dict overwriting the former"""
    result = {}
    for dictionary in dicts:
        result.update(dictionary)
    return result


_CONFIG_CACHE = {}
