"""Agent execution serialization and failure policy."""

from __future__ import annotations

import re
import traceback
from typing import Any

from tenacity import RetryError

from mote.contracts.errors import MoteError
from mote.runtime.logging import logger


def any_to_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    target = value if callable(value) else type(value)
    return f"{target.__module__}.{target.__name__}"


def role_raise_decorator(func):
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except KeyboardInterrupt as exc:
            if self.state.latest_observed_msg:
                self.context_manager.delete(self.state.latest_observed_msg)
            raise Exception(traceback.format_exc(limit=None)) from exc
        except Exception as exc:
            if self.state.latest_observed_msg:
                logger.exception("Role execution failed; removing the newest observed message for recovery")
                self.context_manager.delete(self.state.latest_observed_msg)
            if isinstance(exc, MoteError):
                raise
            if isinstance(exc, RetryError):
                last_error = exc.last_attempt._exception
                if re.match(r"^(openai|httpx)\.", any_to_str(last_error)):
                    raise last_error
            raise Exception(traceback.format_exc(limit=None)) from exc

    return wrapper


__all__ = ["any_to_str", "role_raise_decorator"]
