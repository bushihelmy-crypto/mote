"""Low-cardinality projection for application/model lifecycle events."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from mote.contracts.events.application import (
    ApplicationActivationCasConflict,
    ApplicationActivationCommitted,
    ApplicationActivationRejected,
    ApplicationActivationRequested,
    ApplicationActivationStale,
    ApplicationReadinessFailed,
    ApplicationShutdownTimedOut,
    CompositionCloseFailed,
    GenerationDrainCompleted,
    GenerationDrainTimedOut,
    InferenceTargetCapacityReached,
    InferenceTargetExpired,
    RetiredGenerationCapacityReached,
)

_EVENT_NAMES: dict[type[object], str] = {
    event_type: event_type.name
    for event_type in (
        ApplicationActivationRequested,
        ApplicationActivationCommitted,
        ApplicationActivationRejected,
        ApplicationActivationStale,
        ApplicationActivationCasConflict,
        ApplicationReadinessFailed,
        RetiredGenerationCapacityReached,
        GenerationDrainCompleted,
        GenerationDrainTimedOut,
        InferenceTargetExpired,
        InferenceTargetCapacityReached,
        CompositionCloseFailed,
        ApplicationShutdownTimedOut,
    )
}


@dataclass(frozen=True, slots=True)
class ApplicationMetricsSnapshot:
    event_counts: tuple[tuple[str, int], ...]
    retired_generation_count: int
    inference_target_count: int


class ApplicationMetricsProjection:
    """Projects operational events without generation, route, or credential labels."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._retired_generation_count = 0
        self._inference_target_count = 0

    def observe(self, event: object) -> None:
        event_name = _EVENT_NAMES.get(type(event))
        if event_name is None:
            return
        self._counts[event_name] += 1
        if isinstance(event, RetiredGenerationCapacityReached):
            self._retired_generation_count = event.retired_count
        elif isinstance(event, InferenceTargetCapacityReached):
            self._inference_target_count = event.target_count

    def snapshot(self) -> ApplicationMetricsSnapshot:
        return ApplicationMetricsSnapshot(
            event_counts=tuple(sorted(self._counts.items())),
            retired_generation_count=self._retired_generation_count,
            inference_target_count=self._inference_target_count,
        )


__all__ = ["ApplicationMetricsProjection", "ApplicationMetricsSnapshot"]
