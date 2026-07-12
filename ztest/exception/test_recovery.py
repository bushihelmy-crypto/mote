"""Unit tests for the domain-agnostic :class:`RecoveryRunner` skeleton."""
from __future__ import annotations

import asyncio

import pytest

from metagpt.common.exception import (
    MetaGPTError,
    NonRetryableError,
    RecoveryAction,
    RecoveryRunner,
    RetryableError,
)

pytestmark = pytest.mark.asyncio


class _CompressError(MetaGPTError):
    """A typed error whose recovery hint is COMPRESS (for testing dispatch)."""

    default_recovery = RecoveryAction.COMPRESS


class _FallbackError(MetaGPTError):
    default_recovery = RecoveryAction.FALLBACK


class TestSuccess:
    async def test_returns_result_no_error(self):
        async def call():
            return 42

        runner = RecoveryRunner()
        assert await runner.run(call) == 42

    async def test_recover_then_succeed(self):
        attempts = []

        async def call():
            attempts.append(1)
            if len(attempts) == 1:
                raise _CompressError("too big")
            return "ok"

        recovered = []

        async def compress(exc):
            recovered.append(exc)
            return True

        runner = RecoveryRunner({RecoveryAction.COMPRESS: compress})
        assert await runner.run(call) == "ok"
        assert len(attempts) == 2
        assert len(recovered) == 1


class TestReRaise:
    async def test_retry_action_reraises(self):
        """RETRY is owned by the lower tenacity loop -> runner re-raises."""

        class _Transient(RetryableError):
            pass  # retryable -> recovery == RETRY

        async def call():
            raise _Transient("blip")

        runner = RecoveryRunner({RecoveryAction.COMPRESS: _never})
        with pytest.raises(_Transient):
            await runner.run(call)

    async def test_abort_action_reraises(self):
        class _Permanent(NonRetryableError):
            pass  # non-retryable -> recovery == ABORT

        async def call():
            raise _Permanent("nope")

        runner = RecoveryRunner({RecoveryAction.COMPRESS: _never})
        with pytest.raises(_Permanent):
            await runner.run(call)

    async def test_missing_strategy_reraises(self):
        async def call():
            raise _CompressError("too big")

        runner = RecoveryRunner({})  # no COMPRESS strategy registered
        with pytest.raises(_CompressError):
            await runner.run(call)

    async def test_strategy_returns_false_reraises(self):
        async def call():
            raise _CompressError("too big")

        async def cannot(exc):
            return False

        runner = RecoveryRunner({RecoveryAction.COMPRESS: cannot})
        with pytest.raises(_CompressError):
            await runner.run(call)

    async def test_non_metagpt_error_propagates(self):
        """A plain (non-typed) exception is never caught by the runner."""

        async def call():
            raise ValueError("untyped")

        runner = RecoveryRunner({RecoveryAction.COMPRESS: _always})
        with pytest.raises(ValueError):
            await runner.run(call)


class TestBudget:
    async def test_exhausts_after_max_recoveries(self):
        attempts = []

        async def call():
            attempts.append(1)
            raise _CompressError("still too big")

        async def always(exc):
            return True

        runner = RecoveryRunner({RecoveryAction.COMPRESS: always}, max_recoveries=2)
        with pytest.raises(_CompressError):
            await runner.run(call)
        # initial attempt + 2 recovered retries = 3 calls; the 3rd failure has no budget
        assert len(attempts) == 3

    async def test_cancelled_error_propagates(self):
        async def call():
            raise asyncio.CancelledError()

        runner = RecoveryRunner({RecoveryAction.COMPRESS: _always})
        with pytest.raises(asyncio.CancelledError):
            await runner.run(call)


class TestDispatchSelectsByAction:
    async def test_dispatches_to_matching_action(self):
        seen = {"compress": 0, "fallback": 0}

        async def call():
            # First fail with COMPRESS, then FALLBACK, then succeed.
            n = seen["compress"] + seen["fallback"]
            if n == 0:
                raise _CompressError("c")
            if n == 1:
                raise _FallbackError("f")
            return "done"

        async def compress(exc):
            seen["compress"] += 1
            return True

        async def fallback(exc):
            seen["fallback"] += 1
            return True

        runner = RecoveryRunner(
            {RecoveryAction.COMPRESS: compress, RecoveryAction.FALLBACK: fallback}
        )
        assert await runner.run(call) == "done"
        assert seen == {"compress": 1, "fallback": 1}


class TestUntypedExceptionDispatch:
    """Untyped (non-``MetaGPTError``) exceptions are classified by is_retryable.

    This is the unification point: a bare ``ConnectionError`` has no ``.recovery``
    hint, so the runner derives RETRY (transient) / ABORT (permanent) via the
    generic predicate — letting one loop serve callers that raise stdlib/vendor
    exceptions (bggraph nodes) as well as typed-error callers (LLM).
    """

    async def test_retryable_untyped_dispatches_to_retry_strategy(self):
        attempts = []

        async def call():
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("transient")  # is_retryable -> True -> RETRY
            return "ok"

        retried = []

        async def retry(exc):
            retried.append(exc)
            return True

        runner = RecoveryRunner({RecoveryAction.RETRY: retry})
        assert await runner.run(call) == "ok"
        assert len(attempts) == 2
        assert len(retried) == 1

    async def test_retryable_untyped_without_strategy_reraises(self):
        async def call():
            raise ConnectionError("transient")

        runner = RecoveryRunner({})  # no RETRY strategy registered
        with pytest.raises(ConnectionError):
            await runner.run(call)

    async def test_non_retryable_untyped_aborts(self):
        attempts = []

        async def call():
            attempts.append(1)
            raise ValueError("permanent")  # is_retryable -> False -> ABORT

        async def retry(exc):
            return True

        # Even with a RETRY strategy, a permanent error aborts (never retried).
        runner = RecoveryRunner({RecoveryAction.RETRY: retry})
        with pytest.raises(ValueError):
            await runner.run(call)
        assert len(attempts) == 1  # failed fast, no retry

    async def test_retry_budget_bounds_untyped_retries(self):
        attempts = []

        async def call():
            attempts.append(1)
            raise ConnectionError("always transient")

        async def retry(exc):
            return True

        runner = RecoveryRunner({RecoveryAction.RETRY: retry}, max_recoveries=2)
        with pytest.raises(ConnectionError):
            await runner.run(call)
        # initial attempt + 2 recovered retries = 3 calls
        assert len(attempts) == 3


async def _never(exc):
    return False


async def _always(exc):
    return True


# ---------------------------------------------------------------------------
# Bus emission: the runner mirrors each recovery decision as a RecoveryEvent
# ---------------------------------------------------------------------------


class _RecordingBusSub:
    """Records every RecoveryEvent seen on the bus (observation-only)."""

    priority = 50

    def __init__(self):
        self.events = []

    async def handle(self, event):
        from metagpt.common.events import RecoveryEvent

        if isinstance(event, RecoveryEvent):
            self.events.append(event)
        return None


def _bus_with_recorder():
    from metagpt.common.events import EventBus

    bus = EventBus()
    rec = _RecordingBusSub()
    bus.subscribe(rec)
    return bus, rec


class TestRecoveryEmission:
    async def test_successful_recovery_emits_recovered(self):
        from metagpt.common.events import set_bus

        attempts = []

        async def call():
            attempts.append(1)
            if len(attempts) == 1:
                raise _CompressError("too big")
            return "ok"

        bus, rec = _bus_with_recorder()
        runner = RecoveryRunner({RecoveryAction.COMPRESS: _always})
        with set_bus(bus):
            assert await runner.run(call) == "ok"
        assert len(rec.events) == 1
        e = rec.events[0]
        assert e.phase == "recovered" and e.action == "compress" and e.attempt == 1
        assert e.error_type == "_CompressError"

    async def test_abort_emits_give_up(self):
        from metagpt.common.events import set_bus

        class _Permanent(NonRetryableError):
            pass

        async def call():
            raise _Permanent("nope")

        bus, rec = _bus_with_recorder()
        runner = RecoveryRunner({RecoveryAction.COMPRESS: _always})
        with set_bus(bus):
            with pytest.raises(_Permanent):
                await runner.run(call)
        assert len(rec.events) == 1 and rec.events[0].phase == "give_up"

    async def test_missing_strategy_emits_give_up(self):
        from metagpt.common.events import set_bus

        async def call():
            raise _CompressError("too big")

        bus, rec = _bus_with_recorder()
        runner = RecoveryRunner({})
        with set_bus(bus):
            with pytest.raises(_CompressError):
                await runner.run(call)
        assert len(rec.events) == 1 and rec.events[0].phase == "give_up"
        assert rec.events[0].action == "compress"

    async def test_strategy_false_emits_give_up(self):
        from metagpt.common.events import set_bus

        async def call():
            raise _CompressError("too big")

        bus, rec = _bus_with_recorder()
        runner = RecoveryRunner({RecoveryAction.COMPRESS: _never})
        with set_bus(bus):
            with pytest.raises(_CompressError):
                await runner.run(call)
        assert len(rec.events) == 1 and rec.events[0].phase == "give_up"

    async def test_budget_exhausted_emits_recovered_then_give_up(self):
        from metagpt.common.events import set_bus

        async def call():
            raise _CompressError("still too big")

        bus, rec = _bus_with_recorder()
        runner = RecoveryRunner({RecoveryAction.COMPRESS: _always}, max_recoveries=2)
        with set_bus(bus):
            with pytest.raises(_CompressError):
                await runner.run(call)
        phases = [e.phase for e in rec.events]
        # 2 successful recoveries, then the 3rd failure has no budget -> give_up
        assert phases == ["recovered", "recovered", "give_up"]
        assert [e.attempt for e in rec.events] == [1, 2, 2]

    async def test_unbound_bus_no_emit_no_error(self):
        attempts = []

        async def call():
            attempts.append(1)
            if len(attempts) == 1:
                raise _CompressError("too big")
            return "ok"

        # No set_bus -> emit is a no-op; behaviour is unchanged.
        runner = RecoveryRunner({RecoveryAction.COMPRESS: _always})
        assert await runner.run(call) == "ok"

    async def test_cancelled_error_passes_through_without_emit(self):
        from metagpt.common.events import set_bus

        async def call():
            raise asyncio.CancelledError()

        bus, rec = _bus_with_recorder()
        runner = RecoveryRunner({RecoveryAction.COMPRESS: _always})
        with set_bus(bus):
            with pytest.raises(asyncio.CancelledError):
                await runner.run(call)
        assert rec.events == []
