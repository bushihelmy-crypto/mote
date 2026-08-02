"""Domain-agnostic retry / recovery / failover loop driven by recovery hints.

This is the *skeleton* shared by every layer that wants retry + failover (LLM,
tools, bggraph nodes): a call is run, and when it raises the loop resolves a
:class:`RecoveryAction` for the exception and dispatches to an injected
**strategy** for that action. A strategy mutates whatever domain state it
captured (via closures), returns ``True`` if it repaired the situation, and the
loop retries; otherwise the error propagates.

Resolving the action (:meth:`RecoveryRunner._action_for`) is the single point
that unifies the codebase's two historical retry judgements into one:

- a typed :class:`MoteError` carries an explicit ``recovery`` hint
  (RETRY / ABORT / COMPRESS / ROTATE_CREDENTIAL / FALLBACK / …);
- an untyped exception (bare ``ConnectionError`` / ``TimeoutError`` / a vendor
  SDK error) is classified by the generic :func:`is_retryable` predicate — the
  single source of truth for "is this transient?" — into RETRY (transient) or
  ABORT (permanent).

The exception layer is a leaf and can NOT act on a recovery hint itself (acting
needs business capabilities — compress messages, rotate a key, swap a provider,
sleep-and-rerun a node — which live in upper layers and would create a
``common -> business -> common`` cycle). This runner sidesteps that: it owns
only the **control flow** (try / classify / dispatch / retry / budget) and never
imports a business module (``is_retryable`` is imported lazily inside
``_action_for`` to keep the module body a pure leaf). Each caller supplies a
``{RecoveryAction: strategy}`` registry built from its own injected
capabilities, so the same loop serves LLM requests, tool calls and graph nodes.

Division of labour:

- ``ABORT`` always re-raises (a permanent failure is surfaced to the caller);
  it is never dispatched, even if a strategy is registered for it.
- ``RETRY`` and every other action dispatch through ``strategies[action]``. A
  missing strategy (or one returning ``False``) degrades to a re-raise. So a
  caller with a lower tenacity loop simply omits RETRY (it re-raises here and is
  handled below), while a caller that owns its own retry (bggraph) registers a
  RETRY strategy that backs off and re-runs.
- An empty registry makes the runner behaviourally identical to the un-wrapped
  call (every hint re-raises).
- ``max_recoveries`` bounds the total number of successful recoveries to stop a
  pathological recover-fail-recover cycle.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Mapping, Optional

from mote.contracts.events.telemetry import RecoveryEvent
from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.foundation.errors.codes import RecoveryAction
from mote.runtime.events.context import observe_event
from mote.runtime.resilience.error_classification import is_retryable
from mote.runtime.telemetry.logging import logger

# A no-arg coroutine factory: each invocation issues one attempt and returns its result.
Call = Callable[[], Awaitable]
# Recover from ``exc`` (mutating captured domain state); return True if recovered.
# ``exc`` is any ``Exception`` — a typed ``MoteError`` or an untyped vendor error.
RecoveryStrategy = Callable[[Exception], Awaitable[bool]]


class RecoveryRunner:
    """Run a call under a recovery loop dispatched by ``exc.recovery`` hints.

    Args:
        strategies: ``{RecoveryAction: strategy}`` registry. A strategy is an
            async callable ``(exc) -> bool`` that repairs the situation (via the
            domain state it closed over) and returns whether it succeeded.
        max_recoveries: Cap on total successful recoveries before giving up.
    """

    def __init__(
        self,
        strategies: Optional[Mapping[RecoveryAction, RecoveryStrategy]] = None,
        *,
        max_recoveries: int = 3,
    ) -> None:
        self._strategies = dict(strategies or {})
        self.max_recoveries = max_recoveries

    async def run(self, call: Call):
        """Run ``call``; on any exception, resolve a hint and apply a strategy.

        The exception is classified by :meth:`_action_for` (typed → its
        ``recovery`` hint; untyped → ``is_retryable``). ``ABORT`` always
        re-raises; otherwise the registered strategy for the action is applied
        and the call retried. A missing/failed strategy or an exhausted budget
        re-raises. ``asyncio.CancelledError`` (a ``BaseException``) is never
        caught and propagates immediately.
        """
        recoveries = 0
        while True:
            try:
                return await call()
            except Exception as exc:  # noqa: BLE001 — classify, then dispatch or re-raise
                action = self._action_for(exc)
                if action is RecoveryAction.ABORT:
                    # Permanent failure — surface to the caller.
                    await self._emit_recovery("give_up", action, recoveries, exc)
                    raise
                if recoveries >= self.max_recoveries:
                    await self._emit_recovery("give_up", action, recoveries, exc)
                    raise
                strategy = self._strategies.get(action)
                if strategy is None:
                    # No strategy for this hint (e.g. RETRY when the caller
                    # relies on a lower tenacity loop) — re-raise.
                    await self._emit_recovery("give_up", action, recoveries, exc)
                    raise
                if not await strategy(exc):
                    await self._emit_recovery("give_up", action, recoveries, exc)
                    raise
                recoveries += 1
                await self._emit_recovery("recovered", action, recoveries, exc)

    @staticmethod
    async def _emit_recovery(phase: str, action: RecoveryAction, attempt: int, exc: BaseException) -> None:
        """Mirror a recovery decision onto active Telemetry (best-effort).

        Observation-only: the loop's own re-raise/retry is the real outcome.
        Emission failure never perturbs the recovery flow. It is a no-op when no
        telemetry runtime is bound (standalone use and tests).
        """
        try:
            await observe_event(
                RecoveryEvent(
                    phase=phase,
                    action=getattr(action, "value", str(action)),
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            )
        except Exception as exc:  # noqa: BLE001 — emitting must never break recovery
            logger.debug(f"recovery: RecoveryEvent emit failed: {exc}")

    @staticmethod
    def _action_for(exc: BaseException) -> RecoveryAction:
        """Resolve the recovery hint for *exc* — the unified retry judgement.

        A typed :class:`MoteError` carries an explicit ``recovery`` hint
        (which may be COMPRESS / ROTATE_CREDENTIAL / FALLBACK etc., not just
        RETRY/ABORT). An untyped exception falls back to the generic
        :func:`is_retryable` predicate, deriving RETRY (transient) or ABORT
        (permanent). ``is_retryable`` is imported lazily so this module stays a
        pure leaf (importing it eagerly would drag in unrelated runtime machinery).
        """
        if isinstance(exc, MoteError):
            return exc.recovery

        return RecoveryAction.RETRY if is_retryable(exc) else RecoveryAction.ABORT
