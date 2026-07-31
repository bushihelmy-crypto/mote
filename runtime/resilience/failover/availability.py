"""Epoch-fenced availability breaker for resource admission."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque

from mote.contracts.config.model.breaker import BreakerConfig
from mote.runtime.resilience import MAX_WINDOW_ENTRIES, BreakerState

_Outcome = tuple[float, bool]


class AvailabilityPermit:
    """One admission claim whose outcome may affect only its breaker epoch."""

    __slots__ = (
        "_breaker",
        "_epoch",
        "_lease_deadline",
        "_probe_id",
        "_settled",
    )

    def __init__(
        self,
        breaker: "AvailabilityBreaker",
        *,
        epoch: int,
        probe_id: int | None,
        lease_deadline: float,
    ) -> None:
        self._epoch = epoch
        self._probe_id = probe_id
        self._lease_deadline = lease_deadline
        self._breaker = breaker
        self._settled = False

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def probe_id(self) -> int | None:
        return self._probe_id

    @property
    def lease_deadline(self) -> float:
        return self._lease_deadline

    def succeed(self) -> None:
        self._settle(True)

    def fail(self) -> None:
        self._settle(False)

    def abandon(self) -> None:
        self._settle(None)

    def _settle(self, success: bool | None) -> None:
        if self._settled:
            raise RuntimeError("availability permit already settled")
        self._settled = True
        self._breaker._settle(
            epoch=self._epoch,
            probe_id=self._probe_id,
            success=success,
        )


class AvailabilityBreaker:
    """Sliding-window breaker with epoch fencing and leased recovery probes.

    The object is intentionally single-loop and lock-free. Every admitted call
    receives a permit stamped with the current epoch. A transition increments
    that epoch, so a late result from an older generation cannot mutate current
    health. Half-open probe claims are individually leased to their attempt
    deadline plus a small grace period.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or BreakerConfig()
        if self.config.half_open_success_quorum > self.config.half_open_max_probes:
            raise ValueError("half_open_success_quorum cannot exceed half_open_max_probes")
        if self.config.half_open_success_quorum < 1:
            raise ValueError("half_open_success_quorum must be positive")
        if self.config.probe_grace_seconds < 0:
            raise ValueError("probe_grace_seconds cannot be negative")
        self._clock = clock or time.monotonic
        self._state = BreakerState.CLOSED
        self._epoch = 0
        self._window: Deque[_Outcome] = deque()
        self._opened_at = 0.0
        self._next_probe_id = 0
        self._active_probes: dict[int, float] = {}
        self._probe_successes = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def epoch(self) -> int:
        return self._epoch

    def acquire(self, *, attempt_deadline: float) -> AvailabilityPermit | None:
        """Claim a permit without crossing the owning attempt's time boundary."""

        lease_deadline = attempt_deadline + self.config.probe_grace_seconds
        if not self.config.enabled:
            return AvailabilityPermit(
                self,
                epoch=self._epoch,
                probe_id=None,
                lease_deadline=lease_deadline,
            )

        now = self._clock()
        if self._state is BreakerState.CLOSED:
            return AvailabilityPermit(
                self,
                epoch=self._epoch,
                probe_id=None,
                lease_deadline=lease_deadline,
            )
        if self._state is BreakerState.OPEN:
            if now - self._opened_at < self.config.open_seconds:
                return None
            self._begin_half_open()

        self._reap_expired_probes(now)
        if len(self._active_probes) >= self.config.half_open_max_probes:
            return None
        self._next_probe_id += 1
        probe_id = self._next_probe_id
        self._active_probes[probe_id] = lease_deadline
        return AvailabilityPermit(
            self,
            epoch=self._epoch,
            probe_id=probe_id,
            lease_deadline=lease_deadline,
        )

    def error_rate(self) -> float:
        self._evict(self._clock())
        if not self._window:
            return 0.0
        failures = sum(1 for _, is_failure in self._window if is_failure)
        return failures / len(self._window)

    def _settle(
        self,
        *,
        epoch: int,
        probe_id: int | None,
        success: bool | None,
    ) -> None:
        if not self.config.enabled or epoch != self._epoch:
            return
        if probe_id is None:
            if self._state is not BreakerState.CLOSED or success is None:
                return
            self._record_closed(success)
            return
        if self._state is not BreakerState.HALF_OPEN:
            return

        lease_deadline = self._active_probes.get(probe_id)
        if lease_deadline is None:
            return
        if self._clock() >= lease_deadline:
            self._active_probes.pop(probe_id, None)
            return
        self._active_probes.pop(probe_id, None)
        if success is None:
            return
        if not success:
            self._trip()
            return
        self._probe_successes += 1
        if self._probe_successes >= self.config.half_open_success_quorum:
            self._close()

    def _record_closed(self, success: bool) -> None:
        now = self._clock()
        self._window.append((now, not success))
        if len(self._window) > MAX_WINDOW_ENTRIES:
            self._window.popleft()
        self._evict(now)
        if len(self._window) < self.config.min_samples:
            return
        failures = sum(1 for _, is_failure in self._window if is_failure)
        if failures / len(self._window) >= self.config.error_rate_threshold:
            self._trip()

    def _reap_expired_probes(self, now: float) -> None:
        expired = [probe_id for probe_id, deadline in self._active_probes.items() if now >= deadline]
        for probe_id in expired:
            self._active_probes.pop(probe_id, None)

    def _evict(self, now: float) -> None:
        cutoff = now - self.config.window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _trip(self) -> None:
        self._epoch += 1
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._active_probes.clear()
        self._probe_successes = 0

    def _begin_half_open(self) -> None:
        self._epoch += 1
        self._state = BreakerState.HALF_OPEN
        self._active_probes.clear()
        self._probe_successes = 0

    def _close(self) -> None:
        self._epoch += 1
        self._state = BreakerState.CLOSED
        self._window.clear()
        self._active_probes.clear()
        self._probe_successes = 0


__all__ = ["AvailabilityBreaker", "AvailabilityPermit"]
