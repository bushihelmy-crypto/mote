"""Compile Product Agent governance input into the Orchestration policy."""

from __future__ import annotations

from mote.orchestration.agents.turn_queue.scheduling import RootTurnWeight, TurnSchedulingConfig
from mote.product.config.agents import AgentGovernanceConfig


def compile_turn_scheduling(
    config: AgentGovernanceConfig,
    *,
    generation: int,
) -> TurnSchedulingConfig:
    weights = dict(config.root_weights)
    return TurnSchedulingConfig(
        generation=generation,
        root_weights=tuple(RootTurnWeight(root_id, weight) for root_id, weight in sorted(weights.items())),
    )


__all__ = ["compile_turn_scheduling"]
