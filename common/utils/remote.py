"""Remote execution utilities.

The MCP server backend has been removed. ``@remotable`` is now a no-op
passthrough kept only so that decorated call sites don't need modification.
"""

from typing import Any, Callable, Optional


def remotable(_func: Optional[Callable[..., Any]] = None, **_kwargs):
    """No-op decorator (server backend removed). Returns the function unchanged."""
    if _func is not None:
        return _func

    def decorator(func: Callable) -> Callable:
        return func

    return decorator
