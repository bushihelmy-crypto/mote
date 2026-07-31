from typing import Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract


class PersistedLifecycleEvent(FrozenContract):
    schema_version: Literal[1] = 1
    execution_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    receipt_revision: int = Field(ge=1)
    event_type: str = Field(min_length=1)
    payload: bytes
    terminal: bool = False
