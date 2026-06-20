#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Desc   : base environment

import typing
from abc import abstractmethod

from metagpt.common.schema.serialization import BaseSerialization

if typing.TYPE_CHECKING:
    from metagpt.common.schema.messages import Message


class BaseEnvironment(BaseSerialization):
    """Base environment — the message-passing orchestration contract.

    The gym/RL episode interface (``reset``/``observe``/``step``) lives in
    :mod:`metagpt.common.schema.gym_env` (``GymEnvironment``) so this core stays
    free of RL baggage.
    """

    @abstractmethod
    def publish_message(self, message: "Message", peekable: bool = True) -> bool:
        """Distribute the message to the recipients."""

    @abstractmethod
    async def run(self, k=1):
        """Process all task at once"""
