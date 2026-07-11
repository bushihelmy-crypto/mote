"""
Agent loop strategies — the react cycle extracted out of Role.

A loop drives the already-assembled components (think_engine, command_channel,
executor, context_provider, ...) until its terminal condition, so Role degrades
to an assembler + message publisher. Loops receive ONLY reusable components and
plain callables — never the Role itself and never a Role-private callback.

LoopContext packs the RoleSchema knobs + observe-time data a loop needs (so we
don't hand over the whole schema). BaseLoop is the strategy interface; ReActLoop
is the default think→act cycle ported verbatim from Role._react.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mote.common.schema import Message

if TYPE_CHECKING:
    from mote.common.schema import MessageQueue


@dataclass
class LoopContext:
    """Everything the loop needs from the Role — pure data + the msg_buffer ref.

    Constructed by Role from its role_schema/state and handed to the loop, so
    the loop never reaches into the Role or schema directly.
    """

    # React limits
    max_react_loop: int
    max_consecutive_react_limit: int
    memory_k: int

    # Identity
    name: str
    display_name: str
    tools: list[str] = field(default_factory=list)

    # Observe — the loop owns the buffer→filter→commit pipeline
    msg_buffer: "MessageQueue | None" = None
    watch: set = field(default_factory=set)
    enable_memory: bool = True
    observe_all: bool = True


class BaseLoop(ABC):
    """A replaceable agent-loop strategy.

    Subclasses drive the injected components until their own terminal condition
    and return the final response Message. The Role calls run() once per react()
    and stays agnostic to which strategy is in play.
    """

    # The last message the loop observed this run, propagated by the Role into
    # RoleState for recovery (role_raise_decorator reads it). Subclasses set it
    # during observe; the base declares it so the generic read type-checks.
    latest_observed_msg: "Message | None" = None

    @abstractmethod
    async def run(self) -> Message | None:
        """Drive the components until this strategy's terminal condition.

        Returns None when no messages were observed (nothing to do).
        """
