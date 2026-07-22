"""Stable public facades expose user concepts, not runtime infrastructure."""
from __future__ import annotations

import mote.output as output


def test_output_facade_is_small_and_stable() -> None:
    assert output.__all__ == [
        "OutputContract",
        "OutputRetryPolicy",
        "OutputValidator",
        "RunResult",
        "ValidationIssue",
    ]


def test_output_facade_excludes_runtime_infrastructure() -> None:
    for name in (
        "CommitFence",
        "OutputEngine",
        "OutputMigrationRegistry",
        "RunJournal",
        "RunLeaseCoordinator",
    ):
        assert not hasattr(output, name)
