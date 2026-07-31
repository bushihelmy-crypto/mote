"""Runtime lifecycle coordinator for the recoverable model inference window."""

from __future__ import annotations

from typing import Any

from mote.contracts.execution.models import InferenceCheckpointState


class InferenceCheckpoint:
    """Own all mutable state for one in-flight durable think operation."""

    def __init__(self, *, journal_runner: Any | None, memory: Any, inference_engine: Any) -> None:
        self._journal_runner = journal_runner
        self._memory = memory
        self._inference_engine = inference_engine
        self._step_id: str | None = None
        self._reinstated = False
        self._state: InferenceCheckpointState | None = None

    def reinstate(self) -> bool:
        if self._journal_runner is None:
            return False
        candidate = self._journal_runner.reinstate_candidate(self._memory.get())
        if candidate is None:
            return False
        self._step_id, result = candidate
        self._reinstated = True
        self._inference_engine.reinstate(result)
        return True

    @property
    def step_id(self) -> str | None:
        return self._step_id

    @property
    def reinstated(self) -> bool:
        return self._reinstated

    def begin_call(self, state: InferenceCheckpointState) -> None:
        self._state = state
        if self._journal_runner is not None:
            self._step_id = self._journal_runner.begin_think(state)

    def resume(self) -> InferenceCheckpointState | None:
        """Adopt and return an interrupted call identity, when durable."""

        if self._journal_runner is None:
            return None
        candidate = self._journal_runner.resume_candidate()
        if candidate is None:
            return None
        self._step_id, state = candidate
        self._state = state if isinstance(state, InferenceCheckpointState) else InferenceCheckpointState(str(state))
        return self._state

    def refresh(self, state: InferenceCheckpointState) -> None:
        self._state = state
        if self._journal_runner is not None and self._step_id is not None:
            self._journal_runner.update_think(self._step_id, state)

    def adopt_started(self, step_id: str) -> None:
        """Adopt a step begun by a recovery/bootstrap boundary."""
        if self._step_id is not None:
            raise RuntimeError("a think checkpoint is already active")
        self._step_id = step_id

    def record_result(self) -> None:
        if self._journal_runner is None or self._step_id is None or self._reinstated:
            return
        self._journal_runner.complete_think(
            self._step_id,
            self._inference_engine.result,
            self._state,
        )

    def discard(self) -> None:
        step_id = self._take_step()
        if self._journal_runner is not None and step_id is not None:
            self._journal_runner.reap_think(step_id)

    def abort(self) -> None:
        step_id = self._take_step()
        if self._journal_runner is not None and step_id is not None:
            self._journal_runner.fail_think(step_id)

    def _take_step(self) -> str | None:
        step_id, self._step_id = self._step_id, None
        self._reinstated = False
        self._state = None
        return step_id


__all__ = ["InferenceCheckpoint"]
