"""Typed run-output failures."""
from __future__ import annotations

from typing import ClassVar

from mote.common.exception.base import NonRetryableError, RetryableError
from mote.common.exception.codes import ErrorCode


class OutputCorrectionExhaustedError(NonRetryableError):
    """The model exhausted the run's independent output-correction budget."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OUTPUT_CORRECTION_EXHAUSTED

    def __init__(
        self,
        *,
        max_corrections: int,
        candidate_id: str = "",
        issues: tuple = (),
    ) -> None:
        super().__init__(
            "Final output remained invalid after the correction budget was exhausted",
            max_corrections=max_corrections,
            candidate_id=candidate_id,
            issues=[
                {
                    "path": list(issue.path),
                    "code": issue.code,
                    "message": issue.message,
                }
                for issue in issues
            ],
        )


class OutputCommitStateError(NonRetryableError):
    """A caller attempted output commit before a candidate was accepted."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OUTPUT_COMMIT_INVALID_STATE


class OutputValidatorError(NonRetryableError):
    """A validator crashed instead of returning a validation decision."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OUTPUT_VALIDATOR_FAILED


class OutputValidatorUnavailableError(RetryableError):
    """Validation infrastructure explicitly requested a later retry."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OUTPUT_VALIDATOR_UNAVAILABLE


class OutputResumeContractMismatchError(NonRetryableError):
    """Durable output state belongs to a different deployment contract."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OUTPUT_RESUME_CONTRACT_MISMATCH


class OutputCommitFencedError(NonRetryableError):
    """A stale or expired worker attempted to linearize an output commit."""

    default_code: ClassVar[ErrorCode] = ErrorCode.OUTPUT_COMMIT_FENCED


class RunLeaseUnavailableError(RetryableError):
    """Another live worker currently owns the requested run."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RUN_LEASE_UNAVAILABLE


class RunLeaseCoordinatorUnavailableError(RetryableError):
    """The ownership backend could not prove that this worker is current."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RUN_LEASE_COORDINATOR_UNAVAILABLE
