"""Context-window threshold evaluation owned by Runtime history."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    context_window: int
    summary_reserve: int
    autocompact_threshold: int

    def __post_init__(self) -> None:
        if not 0 < self.summary_reserve < self.context_window:
            raise ValueError("summary_reserve must be inside the context window")
        if not 0 < self.autocompact_threshold <= self.effective_window:
            raise ValueError("autocompact_threshold exceeds the effective window")

    @property
    def effective_window(self) -> int:
        return self.context_window - self.summary_reserve


@dataclass(frozen=True, slots=True)
class ContextBudgetEvaluation:
    token_count: int
    percent_left: int
    above_warning: bool
    above_error: bool
    above_autocompact: bool
    at_blocking_limit: bool


def evaluate_context_budget(
    token_count: int,
    policy: ContextBudgetPolicy,
    *,
    autocompact_enabled: bool,
    warning_buffer: int,
    error_buffer: int,
    blocking_buffer: int,
) -> ContextBudgetEvaluation:
    if token_count < 0:
        raise ValueError("token_count must be non-negative")
    threshold = policy.autocompact_threshold if autocompact_enabled else policy.effective_window
    percent_left = max(0, round((threshold - token_count) / threshold * 100))
    return ContextBudgetEvaluation(
        token_count=token_count,
        percent_left=percent_left,
        above_warning=token_count >= threshold - warning_buffer,
        above_error=token_count >= threshold - error_buffer,
        above_autocompact=token_count >= policy.autocompact_threshold,
        at_blocking_limit=token_count >= threshold - blocking_buffer,
    )


__all__ = [
    "ContextBudgetEvaluation",
    "ContextBudgetPolicy",
    "evaluate_context_budget",
]
