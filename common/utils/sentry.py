import asyncio
import functools
from typing import Any, Callable, Optional, TypeVar

from mote.common.config.loader import load_config
from mote.common.logs import logger

ReturnType = TypeVar("ReturnType")

# Global flag to track if Sentry has been initialized
_sentry_initialized = False


def _init_sentry() -> bool:
    """Initialize Sentry SDK if not already initialized and enabled in config.

    Returns:
        bool: True if Sentry is enabled and initialized, False otherwise
    """
    global _sentry_initialized

    try:
        config = load_config()
        sentry_config = config.sentry

        if not sentry_config.enable:
            return False

        if _sentry_initialized:
            return True

        if not sentry_config.dsn:
            logger.warning("Sentry is enabled but DSN is not configured")
            return False

        import sentry_sdk

        sentry_sdk.init(
            dsn=sentry_config.dsn,
            environment=sentry_config.environment,
            send_default_pii=sentry_config.send_default_pii,
        )

        _sentry_initialized = True
        logger.info(f"Sentry initialized successfully with environment: {sentry_config.environment}")
        return True

    except Exception as e:
        logger.error(f"Failed to initialize Sentry: {e}")
        return False


def capture_errors(
    _func: Optional[Callable[..., ReturnType]] = None,
    *,
    reraise: bool = True,
    extra_data: Optional[dict] = None,
    tags: Optional[dict] = None,
    level: str = "error",
) -> Callable[..., Any]:
    """Decorator to capture errors and send them to Sentry.

    Args:
        _func: The function to decorate (for use without parentheses)
        reraise: Whether to reraise the exception after capturing it
        extra_data: Additional data to send with the error
        tags: Tags to add to the Sentry event
        level: Sentry level (debug, info, warning, error, fatal)

    Returns:
        Decorated function

    Example:
        @capture_errors
        async def my_function():
            # This will capture any errors and send them to Sentry
            pass

        @capture_errors(reraise=False, tags={"component": "llm"})
        def another_function():
            # This will capture errors but not reraise them
            pass
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                await _handle_exception(e, func, args, kwargs, extra_data, tags, level)
                if not reraise:
                    return None
                raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                _handle_exception_sync(e, func, args, kwargs, extra_data, tags, level)
                if not reraise:
                    return None
                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator if _func is None else decorator(_func)


async def _handle_exception(
    exception: Exception,
    func: Callable,
    args: tuple,
    kwargs: dict,
    extra_data: Optional[dict],
    tags: Optional[dict],
    level: str,
) -> None:
    """Handle exception asynchronously."""
    _handle_exception_sync(exception, func, args, kwargs, extra_data, tags, level)


def _handle_exception_sync(
    exception: Exception,
    func: Callable,
    args: tuple,
    kwargs: dict,
    extra_data: Optional[dict],
    tags: Optional[dict],
    level: str,
) -> None:
    """Handle exception synchronously."""
    if not _init_sentry():
        return

    try:
        import sentry_sdk

        tags = tags or {}
        extra_data = extra_data or {}

        # Get config extra data and merge with passed extra_data (higher priority)
        config = load_config()
        merged_extra_data = (config.sentry.extra_data or {}).copy()
        if extra_data:
            merged_extra_data.update(extra_data)

        # Set additional context
        with sentry_sdk.configure_scope() as scope:
            # Add function context
            scope.set_context(
                "function",
                {
                    "name": func.__name__,
                    "module": func.__module__,
                    "args_count": len(args),
                    "kwargs_keys": list(kwargs.keys()) if kwargs else [],
                },
            )

            # Add custom tags
            for key, value in tags.items():
                scope.set_tag(key, str(value))

            # Add extra data
            for key, value in merged_extra_data.items():
                scope.set_extra(key, value)

            # Set level
            scope.level = level

            # Capture the exception
            sentry_sdk.capture_exception(exception)

    except Exception as sentry_error:
        logger.error(f"Failed to send error to Sentry: {sentry_error}")
