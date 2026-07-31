"""Transient state owned by one Kernel Agent run."""

from __future__ import annotations

from dataclasses import dataclass, field

from mote.contracts.model.inference import InferenceResult


@dataclass
class AgentRunState:
    """Non-durable signals shared by Flow and tool-driven stop capabilities."""

    active: bool = False
    last_inference_result: InferenceResult = field(default_factory=InferenceResult)


__all__ = ["AgentRunState"]
