#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Desc   : base environment

import typing
from abc import abstractmethod
from enum import IntEnum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from metagpt.common.schema.serialization import BaseSerialization

if typing.TYPE_CHECKING:
    from metagpt.common.schema.messages import Message


class BaseEnvActionType(IntEnum):
    pass


class BaseEnvAction(BaseModel):
    """env action type and its related params of action functions/apis"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    action_type: int = Field(default=0, description="action type")


class BaseEnvObsType(IntEnum):
    pass


class BaseEnvObsParams(BaseModel):
    """observation params for different EnvObsType to get its observe result"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    obs_type: int = Field(default=0, description="observation type")


class BaseEnvironment(BaseSerialization):
    """Base environment"""

    @abstractmethod
    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Implement this to get init observation"""

    @abstractmethod
    def observe(self, obs_params: Optional[BaseEnvObsParams] = None) -> Any:
        """Implement this if you want to get partial observation from the env"""

    @abstractmethod
    def step(self, action: BaseEnvAction) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        """Implement this to feed a action and then get new observation from the env"""

    @abstractmethod
    def publish_message(self, message: "Message", peekable: bool = True) -> bool:
        """Distribute the message to the recipients."""

    @abstractmethod
    async def run(self, k=1):
        """Process all task at once"""
