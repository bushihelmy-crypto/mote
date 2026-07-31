from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    """Base for the product composition configuration."""

    model_config = ConfigDict(extra="forbid")
