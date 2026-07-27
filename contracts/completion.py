"""Provider-independent completion decisions."""
from dataclasses import dataclass
from enum import Enum


class CompletionKind(str, Enum):
    CONTINUE = "continue"
    VALIDATE_CANDIDATE = "validate_candidate"
    COMPLETE = "complete"
    FAIL = "fail"


@dataclass(frozen=True)
class CompletionDecision:
    kind: CompletionKind
    candidate_index: int | None = None
    reason: str = ""
