"""mote.runtime.hook — opt-in agent-lifecycle hook subsystem.

Provides a Codex-compatible hook engine: matcher groups, the JSON
stdin/stdout command contract, deny > ask > allow aggregation, plus an in-process
Python callback path native to an embeddable framework.

The neutral :class:`HookOutcome` is folded into a real ``PermissionDecision`` at
the executor seam (``ToolExecutor.run_command``) — this package never imports
the executor.
"""

from mote.runtime.hook.manager import HookManager
from mote.runtime.hook.types import EMPTY, HookBehavior, HookEvent, HookOutcome, fold

__all__ = [
    "HookManager",
    "HookEvent",
    "HookBehavior",
    "HookOutcome",
    "EMPTY",
    "fold",
]
