"""mote.common.base — the project's base classes, gathered in one place.

These are the *concrete/abstract base classes* meant to be subclassed (as opposed
to the structural Protocols in ``mote.common.interface``). Only base classes
that depend solely on ``mote.common`` + the stdlib live here, so this package
stays importable from anywhere without risking an import cycle.

Submodules import order below is deliberate: zero-dependency bases first, so a
package-level ``from mote.common.base import X`` never observes a partially
initialized module.
"""

from mote.common.base.agent import BaseAgent
from mote.common.base.command_channel import CommandChannel
from mote.common.base.loop import PROCEED, BaseLoop, BudgetVerdict, LoopContext
from mote.common.base.role import BaseRole
from mote.common.base.singleton import Singleton
from mote.common.base.think_engine import BaseThinkEngine

__all__ = [
    "Singleton",
    "BaseRole",
    "BaseAgent",
    "BaseLoop",
    "LoopContext",
    "BudgetVerdict",
    "PROCEED",
    "BaseThinkEngine",
    "CommandChannel",
]
