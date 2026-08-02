"""Canonical base for strict configuration declarations."""

from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = ["ConfigModel"]
