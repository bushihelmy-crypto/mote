from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    """Base for validated model configuration data."""

    model_config = ConfigDict(extra="forbid")
