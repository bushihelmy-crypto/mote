"""Base scorer."""

from abc import ABC, abstractmethod

from mote.memory.procedural_memory.schema import Score
from pydantic import BaseModel, ConfigDict


class BaseScorer(BaseModel, ABC):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @abstractmethod
    async def evaluate(self, req: str, resp: str) -> Score:
        """Evaluates the quality of a response relative to a given request."""
