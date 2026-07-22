"""Retry-backoff wait strategy that honours a provider ``Retry-After``.

``wait_retry_after`` is a tenacity ``wait_base`` subclass: when the attempt that
just failed carries a positive ``retry_after`` (seconds parsed from the HTTP
``Retry-After`` header by ``common.exception.handlers`` and stashed on the typed
``LLMError``), it waits exactly that (capped), honouring the provider's advertised
cool-off; otherwise it delegates to a ``fallback`` wait (default
``wait_random_exponential(min=1, max=60)``, the policy every retry site used
before). The cap bounds a hostile/absurd header value so one bad response can't
park a turn for hours.
"""

from __future__ import annotations

from tenacity import RetryCallState
from tenacity.wait import wait_base, wait_random_exponential

# Never sleep longer than this even if a provider advertises a larger cool-off —
# a misconfigured/hostile header must not park a turn for hours.
MAX_RETRY_AFTER_SECONDS = 300.0


class wait_retry_after(wait_base):
    """Wait the exception's ``retry_after`` (capped) else delegate to ``fallback``."""

    def __init__(self, fallback: wait_base | None = None, max_wait: float = MAX_RETRY_AFTER_SECONDS) -> None:
        self._fallback: wait_base = fallback if fallback is not None else wait_random_exponential(min=1, max=60)
        self._max_wait = max_wait

    def __call__(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        if outcome is not None and outcome.failed:
            retry_after = getattr(outcome.exception(), "retry_after", None)
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                return min(float(retry_after), self._max_wait)
        return self._fallback(retry_state)
