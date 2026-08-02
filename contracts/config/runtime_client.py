"""Approved immutable configuration consumed by Runtime model clients."""

from __future__ import annotations

from pydantic import Field

from mote.contracts.config.base import ConfigModel
from mote.contracts.config.model.breaker import BreakerConfig


class LangfuseActivationSpec(ConfigModel):
    enabled: bool = False
    host: str = "https://cloud.langfuse.com"
    public_key: str = ""
    secret_key: str = ""
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    trace_steps: bool = True


class RuntimeClientActivationSpec(ConfigModel):
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    langfuse: LangfuseActivationSpec = Field(default_factory=LangfuseActivationSpec)


__all__ = ["LangfuseActivationSpec", "RuntimeClientActivationSpec"]
