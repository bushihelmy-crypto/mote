from pydantic import BaseModel, ConfigDict


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
