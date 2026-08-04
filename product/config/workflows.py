"""Product selection of the optional Workflow effect transport."""

from __future__ import annotations

from pydantic import Field

from mote.contracts.config.base import ConfigModel
from mote.contracts.config.tool import TemporalConfig


class WorkflowConfig(ConfigModel):
    temporal_enabled: bool = False
    temporal: TemporalConfig = Field(default_factory=TemporalConfig)


__all__ = ["WorkflowConfig"]
