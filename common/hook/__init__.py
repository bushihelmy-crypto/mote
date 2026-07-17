"""mote.common.hook — opt-in agent-lifecycle hook subsystem.

A bottom-layer package (imports only stdlib + ``common``) so any layer may use
it directly. Provides a Codex-compatible hook engine: matcher groups, the JSON
stdin/stdout command contract, deny > ask > allow aggregation, plus an in-process
Python callback path native to an embeddable framework.

The neutral :class:`HookOutcome` is folded into a real ``PermissionDecision`` at
the executor seam (``ToolExecutor.run_command``) — this package never imports
the executor.
"""

from mote.common.hook.config_source import HOOKS_CONFIG_FILE_NAME, load_global_hooks, merge_hook_configs
from mote.common.hook.manager import HookCallback, HookManager
from mote.common.hook.types import EMPTY, HookBehavior, HookEvent, HookInput, HookOutcome, fold

__all__ = [
    "HookManager",
    "HookCallback",
    "HookEvent",
    "HookBehavior",
    "HookInput",
    "HookOutcome",
    "EMPTY",
    "fold",
    "HOOKS_CONFIG_FILE_NAME",
    "load_global_hooks",
    "merge_hook_configs",
]
