"""Lifecycle coordinator for the recoverable model-think window."""

from __future__ import annotations

from typing import Any


class ThinkCheckpoint:
    """Own all mutable state for one in-flight durable think operation."""

    def __init__(self, *, journal_runner: Any | None, memory: Any, think_engine: Any) -> None:
        self._journal_runner = journal_runner
        self._memory = memory
        self._think_engine = think_engine
        self._step_id: str | None = None
        self._reinstated = False

    def reinstate(self) -> bool:
        if self._journal_runner is None:
            return False
        candidate = self._journal_runner.reinstate_candidate(self._memory.get())
        if candidate is None:
            return False
        self._step_id, result = candidate
        self._reinstated = True
        self._think_engine.reinstate(result)
        return True

    @property
    def step_id(self) -> str | None:
        return self._step_id

    @property
    def reinstated(self) -> bool:
        return self._reinstated

    def begin(self, model_call_id: str) -> str | None:
        if self._journal_runner is not None:
            self._step_id = self._journal_runner.begin_think(model_call_id)
        return self._step_id

    def resume_model_call_id(self) -> str | None:
        """Adopt and return an interrupted call identity, when durable."""

        if self._journal_runner is None:
            return None
        candidate = self._journal_runner.resume_candidate()
        if candidate is None:
            return None
        self._step_id, model_call_id = candidate
        return model_call_id

    def adopt_started(self, step_id: str) -> None:
        """Adopt a step begun by a recovery/bootstrap boundary."""
        if self._step_id is not None:
            raise RuntimeError("a think checkpoint is already active")
        self._step_id = step_id

    def complete(self) -> None:
        if self._journal_runner is None or self._step_id is None or self._reinstated:
            return
        self._journal_runner.complete_think(self._step_id, self._think_engine.result)

    def reap(self) -> None:
        step_id = self._take_step()
        if self._journal_runner is not None and step_id is not None:
            self._journal_runner.reap_think(step_id)

    def fail(self) -> None:
        step_id = self._take_step()
        if self._journal_runner is not None and step_id is not None:
            self._journal_runner.fail_think(step_id)

    def _take_step(self) -> str | None:
        step_id, self._step_id = self._step_id, None
        self._reinstated = False
        return step_id


__all__ = ["ThinkCheckpoint"]
