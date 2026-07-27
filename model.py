"""Stable model selection value for the public Agent facade."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Model:
    """Select a model and optional provider profile without exposing clients."""

    name: str
    provider: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("model name must not be empty")
        object.__setattr__(self, "name", self.name.strip())
        if self.provider is not None:
            provider = self.provider.strip().lower()
            object.__setattr__(self, "provider", provider or None)

    def config_overlay(self) -> dict[str, object]:
        default: dict[str, object] = {"model": self.name}
        if self.provider is not None:
            default["provider"] = self.provider
        return {"models": {"default": default}}


__all__ = ["Model"]
