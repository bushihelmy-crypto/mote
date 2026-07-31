from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    """Base for validated tool configuration data."""

    model_config = ConfigDict(extra="forbid")
