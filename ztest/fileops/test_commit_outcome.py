from __future__ import annotations

import pytest

from mote.contracts.fileops import EditCommitChange, EditCommitOutcome, MutationResult, PathToken, TransactionStatus


def test_edit_commit_outcome_rejects_changes_for_uncommitted_result() -> None:
    result = MutationResult("transaction", TransactionStatus.ABORTED)
    change = EditCommitChange(
        path=PathToken(display="/workspace/a.py", native="/workspace/a.py"),
        old="before",
        new="after",
        post_digest="0" * 64,
    )

    with pytest.raises(ValueError, match="only a committed edit"):
        EditCommitOutcome(result=result, changes=(change,))


def test_edit_commit_outcome_requires_one_change_per_committed_version() -> None:
    result = MutationResult("transaction", TransactionStatus.COMMITTED)
    change = EditCommitChange(
        path=PathToken(display="/workspace/a.py", native="/workspace/a.py"),
        old="before",
        new="after",
        post_digest="0" * 64,
    )

    with pytest.raises(ValueError, match="do not match committed versions"):
        EditCommitOutcome(result=result, changes=(change,))
