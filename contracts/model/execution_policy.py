from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EndpointExecutionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    max_output_tokens: int = Field(default=4096, ge=1)
    temperature_micros: int = Field(default=0, ge=0, le=2_000_000)
    timeout_milliseconds: int = Field(default=600_000, gt=0)
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    calculate_usage: bool = True
    prompt_cache_enabled: bool = True
