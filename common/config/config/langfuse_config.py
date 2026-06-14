from pydantic import Field

from metagpt.common.utils.yaml_model import YamlModel


class LangfuseConfig(YamlModel):
    """Configuration for Langfuse LLM observability tracing.

    Disabled by default; langfuse is an optional dependency only imported when
    enabled. See metagpt.common.observability.langfuse_integration.
    """

    enabled: bool = Field(default=False, description="Enable Langfuse tracing")
    host: str = Field(default="https://cloud.langfuse.com", description="Langfuse host endpoint")
    public_key: str = Field(default="", description="Langfuse public key")
    secret_key: str = Field(default="", description="Langfuse secret key")
    sample_rate: float = Field(default=1.0, description="Trace sampling rate (LANGFUSE_SAMPLE_RATE)")
    trace_steps: bool = Field(default=True, description="Emit think/act/tool spans (3rd layer)")
