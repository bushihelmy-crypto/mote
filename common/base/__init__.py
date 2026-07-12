"""metagpt.common.base — the project's base classes, gathered in one place.

These are the *concrete/abstract base classes* meant to be subclassed (as opposed
to the structural Protocols in ``metagpt.common.interface``). Only base classes
that depend solely on ``metagpt.common`` + the stdlib live here, so this package
stays importable from anywhere without risking an import cycle.

Submodules import order below is deliberate: zero-dependency bases first, so a
package-level ``from metagpt.common.base import X`` never observes a partially
initialized module.
"""

from metagpt.common.base.singleton import Singleton
from metagpt.common.base.role import BaseRole
from metagpt.common.base.agent import BaseAgent
from metagpt.common.base.loop import BaseLoop, LoopContext
from metagpt.common.base.think_engine import BaseThinkEngine
from metagpt.common.base.command_channel import CommandChannel
from metagpt.common.base.postprocess_plugin import BasePostProcessPlugin

__all__ = [
    "Singleton",
    "BaseRole",
    "BaseAgent",
    "BaseLoop",
    "LoopContext",
    "BaseThinkEngine",
    "CommandChannel",
    "BasePostProcessPlugin",
]
