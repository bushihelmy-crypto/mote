from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    """Base for validated conversation configuration data."""

    model_config = ConfigDict(extra="forbid")
