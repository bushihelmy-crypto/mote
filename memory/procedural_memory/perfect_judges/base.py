"""Base perfect judge."""

from abc import ABC, abstractmethod

from mote.memory.procedural_memory.schema import Experience
from pydantic import BaseModel, ConfigDict


class BasePerfectJudge(BaseModel, ABC):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @abstractmethod
    async def is_perfect_exp(self, exp: Experience, serialized_req: str, *args, **kwargs) -> bool:
        """Determine whether the experience is perfect.

        Args:
            exp (Experience): The experience to evaluate.
            serialized_req (str): The serialized request to compare against the experience's request.
        """
