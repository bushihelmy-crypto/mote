"""Provider-independent model inference port."""

from typing import Protocol

from mote.contracts.model.inference import (
    FinalizedInferenceRequest,
    InferenceAttemptFence,
    InferenceIntent,
    InferenceOutcome,
    ResolvedInferenceTarget,
)


class ModelInferencePort(Protocol):
    async def resolve(self, intent: InferenceIntent) -> ResolvedInferenceTarget:
        ...

    async def infer(
        self,
        target: ResolvedInferenceTarget,
        request: FinalizedInferenceRequest,
        attempt: InferenceAttemptFence,
    ) -> InferenceOutcome:
        ...

    async def release(self, target: ResolvedInferenceTarget) -> None:
        ...


__all__ = ["ModelInferencePort"]
