#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Desc   : gym-style (reinforcement-learning) environment interface

"""RL / gym-style environment surface, decoupled from the orchestration core.

These types model the classic OpenAI Gym / Gymnasium episode interface
(``reset`` / ``observe`` / ``step``) and are kept here, separate from the lean
message-passing :class:`~mote.common.schema.env.BaseEnvironment`, so the
multi-agent control plane never carries RL baggage. :class:`GymEnvironment`
extends ``BaseEnvironment`` with the RL methods; implement it only if you need
``reset``/``observe``/``step`` semantics.
"""

from abc import abstractmethod
from enum import IntEnum
from typing import Any, Optional

from mote.common.schema.env import BaseEnvironment
from pydantic import BaseModel, ConfigDict, Field


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


class GymEnvironment(BaseEnvironment):
    """A :class:`BaseEnvironment` extended with the gym episode interface."""

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
