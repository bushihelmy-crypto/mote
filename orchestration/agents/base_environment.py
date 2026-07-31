#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Base environment interface for orchestration agents.

import typing
from abc import abstractmethod

from pydantic import BaseModel, ConfigDict

if typing.TYPE_CHECKING:
    from mote.contracts.conversation import Message


class BaseEnvironment(BaseModel):
    """Base environment — the message-passing orchestration contract.

    The gym/RL episode interface (``reset``/``observe``/``step``) lives in
    a Product-provided environment adapter so this core stays
    free of RL baggage.
    """

    model_config = ConfigDict(extra="forbid")

    @abstractmethod
    def publish_message(self, message: "Message", peekable: bool = True) -> bool:
        """Distribute the message to the recipients."""

    @abstractmethod
    async def run_ready_turns(self, max_turns: int = 1) -> int:
        """Run a bounded number of ready agent turns."""
