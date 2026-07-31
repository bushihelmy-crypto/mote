"""CircuitBreaker — a domain-agnostic sliding-window three-state breaker.

The state machine that turns a stream of success/failure outcomes for ONE
resource into an ``admit`` verdict, so a caller can *stop hammering* a resource
that is failing and give it a bounded window to recover:

    CLOSED    — healthy; every call admitted. Outcomes accumulate in a sliding
                window; a sustained failure rate trips the breaker to OPEN.
    OPEN      — tripped; calls refused (fast fail) for ``open_seconds``. After
                the cool-down a single probe is admitted → HALF_OPEN.
    HALF_OPEN — probing recovery; up to ``half_open_max_probes`` calls admitted.
                A probe success closes the breaker, a probe failure re-opens it.

Deliberately framework-agnostic (no Telemetry, no config-v2, no LLM types): it takes a
:class:`BreakerConfig`, an injected ``clock`` (so tests drive a fake monotonic
time), and an optional ``on_transition`` callback that a higher layer uses to
mirror state changes onto Telemetry. Everything domain-specific — *what*
counts as a failure, *which* resource this guards, *how* transitions are
observed — lives in the caller, not here.

Concurrency model: mote runs one asyncio event loop, so breaker calls are never
truly parallel — plain attribute reads/writes are already atomic between awaits.
No lock, no ``AtomicBool`` mirror (that is a *multi-threaded* Rust concern); the
grok design's lock-free fast path is a no-op here by construction.

Abandoned-probe reclaim ("a timeout for the timeout"): a half-open probe is
*claimed* on admit and only released when its outcome is recorded. If the probing
call is cancelled / crashes before recording, the claim would otherwise pin the
slot forever and wedge the breaker OPEN. So a claim older than ``open_seconds``
(the lease) is deemed abandoned and reclaimed by the next ``admit``.
"""

from __future__ import annotations

import time
from collections import deque
from enum import Enum
from typing import Callable, Deque, Optional, Tuple

from mote.contracts.config.model.breaker import BreakerConfig
from mote.runtime.telemetry.logging import logger

#: Hard cap on retained window entries — a safety valve so a pathological burst
#: of outcomes between two evictions can never grow the deque without bound.
#: (Time-based eviction is the real policy; this only bounds worst-case memory.)
MAX_WINDOW_ENTRIES = 10_000


class BreakerState(str, Enum):
    """The three states of the breaker (``str`` mixin → JSON/log friendly)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


#: One recorded outcome: ``(monotonic_ts, is_failure)``.
_Outcome = Tuple[float, bool]

#: Signature of the optional transition observer: ``(key, old, new, reason)``.
TransitionHook = Callable[[str, BreakerState, BreakerState, str], None]


class CircuitBreaker:
    """A single resource's breaker. Not thread-safe by design (asyncio single-loop).

    Construct one per resource (see :class:`ResourceHealthRegistry` for lazy
    per-key creation). ``key`` is an opaque label passed straight through to the
    transition hook and logs; the breaker never interprets it.
    """

    def __init__(
        self,
        config: Optional[BreakerConfig] = None,
        *,
        key: str = "",
        clock: Callable[[], float] = None,  # type: ignore[assignment]
        on_transition: Optional[TransitionHook] = None,
    ) -> None:
        self.config = config or BreakerConfig()
        self.key = key
        # Injected monotonic clock (never wall-clock — immune to NTP steps and
        # lets tests advance time deterministically). Bound late so the default
        # import stays cheap.
        if clock is None:
            clock = time.monotonic
        self._clock = clock
        self._on_transition = on_transition

        self._state: BreakerState = BreakerState.CLOSED
        self._window: Deque[_Outcome] = deque()
        #: When the breaker last tripped to OPEN (for the cool-down check).
        self._opened_at: float = 0.0
        #: Count of in-flight half-open probes (claimed, not yet recorded).
        self._probes: int = 0
        #: Monotonic ts of the most recent probe claim (for abandoned-lease check).
        self._probe_claimed_at: float = 0.0

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        """Current state. May mutate OPEN→HALF_OPEN lazily via :meth:`admit`."""
        return self._state

    def admit(self) -> bool:
        """Return whether a call to the guarded resource should proceed.

        CLOSED → always ``True``. OPEN → ``False`` until the cool-down elapses,
        then transitions to HALF_OPEN and admits a probe. HALF_OPEN → admits up
        to ``half_open_max_probes`` concurrent probes (reclaiming abandoned ones).
        An ``enabled=False`` breaker always admits.
        """
        if not self.config.enabled:
            return True

        now = self._clock()
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            if now - self._opened_at >= self.config.open_seconds:
                self._transition(BreakerState.HALF_OPEN, "cool-down elapsed")
                self._probes = 0
                return self._claim_probe(now)
            return False
        # HALF_OPEN
        return self._claim_probe(now)

    def record(self, success: bool) -> None:
        """Record the outcome of an admitted call. No-op when ``enabled=False``.

        In HALF_OPEN a success closes the breaker and a failure re-opens it (the
        probe slot is released either way). In CLOSED the outcome joins the
        sliding window and may trip the breaker. In OPEN outcomes are ignored
        (a call that raced past a just-tripped breaker must not skew recovery).
        """
        if not self.config.enabled:
            return

        now = self._clock()
        if self._state is BreakerState.HALF_OPEN:
            self._probes = max(0, self._probes - 1)
            if success:
                self._close("probe succeeded")
            else:
                self._trip("probe failed")
            return
        if self._state is BreakerState.OPEN:
            return
        # CLOSED
        self._window.append((now, not success))
        if len(self._window) > MAX_WINDOW_ENTRIES:
            self._window.popleft()
        self._evict(now)
        if self._should_trip():
            self._trip("failure-rate threshold exceeded")

    def abandon(self) -> None:
        """Release an admitted half-open probe without recording an outcome."""

        if not self.config.enabled:
            return
        if self._state is BreakerState.HALF_OPEN:
            self._probes = max(0, self._probes - 1)

    def error_rate(self) -> float:
        """Current failure ratio over the (evicted) window; ``0.0`` if empty."""
        self._evict(self._clock())
        total = len(self._window)
        if total == 0:
            return 0.0
        failures = sum(1 for _, is_failure in self._window if is_failure)
        return failures / total

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _claim_probe(self, now: float) -> bool:
        """Try to claim a half-open probe slot; reclaim an abandoned one if any."""
        limit = max(1, self.config.half_open_max_probes)
        if self._probes < limit:
            self._probes += 1
            self._probe_claimed_at = now
            return True
        # All slots claimed — but a claim never released within the lease means
        # its probing call was cancelled/crashed; reclaim it rather than wedge.
        if now - self._probe_claimed_at >= self.config.open_seconds:
            logger.warning(f"CircuitBreaker[{self.key}] reclaiming abandoned half-open probe")
            self._probe_claimed_at = now
            return True
        return False

    def _evict(self, now: float) -> None:
        """Drop window entries older than ``window_seconds``."""
        cutoff = now - self.config.window_seconds
        window = self._window
        while window and window[0][0] < cutoff:
            window.popleft()

    def _should_trip(self) -> bool:
        """True once the window holds ``>= min_samples`` and the rate is at/over threshold."""
        total = len(self._window)
        if total < self.config.min_samples:
            return False
        failures = sum(1 for _, is_failure in self._window if is_failure)
        return failures / total >= self.config.error_rate_threshold

    def _trip(self, reason: str) -> None:
        self._opened_at = self._clock()
        self._probes = 0
        self._transition(BreakerState.OPEN, reason)

    def _close(self, reason: str) -> None:
        self._window.clear()
        self._probes = 0
        self._transition(BreakerState.CLOSED, reason)

    def _transition(self, new: BreakerState, reason: str) -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        logger.info(f"CircuitBreaker[{self.key}] {old.value} -> {new.value} ({reason})")
        if self._on_transition is not None:
            try:
                self._on_transition(self.key, old, new, reason)
            except Exception as e:  # observation must never break the breaker
                logger.warning(f"CircuitBreaker[{self.key}] on_transition hook raised: {e}")


__all__ = ["CircuitBreaker", "BreakerState", "TransitionHook", "MAX_WINDOW_ENTRIES"]
