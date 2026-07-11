"""Remote execution utilities.

The MCP server backend has been removed. ``@remotable`` is now a no-op
passthrough kept only so that decorated call sites don't need modification.
"""

from typing import Any, Callable, Optional, TypeVar, overload

F = TypeVar("F", bound=Callable[..., Any])


@overload
def remotable(_func: F) -> F:
    ...


@overload
def remotable(_func: None = ..., **_kwargs: Any) -> Callable[[F], F]:
    ...


def remotable(_func: Optional[F] = None, **_kwargs):
    """No-op decorator (server backend removed). Returns the function unchanged."""
    if _func is not None:
        return _func

    def decorator(func: F) -> F:
        return func

    return decorator
