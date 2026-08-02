"""Product-owned Agent governance configuration syntax."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from mote.contracts.config.base import ConfigModel


class AgentGovernanceConfig(ConfigModel):
    logical_agents: int = Field(default=64, ge=1, le=100_000)
    resident_incarnations: int = Field(default=16, ge=1, le=4_096)
    turn_queue_capacity: int = Field(default=256, ge=1, le=100_000)
    concurrent_turns: int = Field(default=4, ge=1, le=1_024)
    root_weights: dict[str, int] = Field(default_factory=dict)
    max_depth: int = Field(default=8, ge=1, le=64)
    root_token_budget: int = Field(default=10_000_000, ge=1)
    root_cost_micro_usd_budget: int = Field(default=1_000_000_000, ge=1)
    child_token_reservation: int = Field(default=100_000, ge=1)
    child_cost_micro_usd_reservation: int = Field(default=10_000_000, ge=1)

    @field_validator("root_weights")
    @classmethod
    def _bounded_root_weights(cls, values: dict[str, int]) -> dict[str, int]:
        if any(type(root_id) is not str or not root_id for root_id in values):
            raise ValueError("Agent governance root weight identities must be non-empty")
        if any(type(weight) is not int or not 1 <= weight <= 64 for weight in values.values()):
            raise ValueError("Agent governance root weights must be between 1 and 64")
        return values

    @model_validator(mode="after")
    def _child_reservations_fit_root_budget(self) -> "AgentGovernanceConfig":
        if self.child_token_reservation > self.root_token_budget:
            raise ValueError("child token reservation exceeds the root budget")
        if self.child_cost_micro_usd_reservation > self.root_cost_micro_usd_budget:
            raise ValueError("child cost reservation exceeds the root budget")
        return self


__all__ = ["AgentGovernanceConfig"]
