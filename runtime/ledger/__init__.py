"""Runtime append-only ledger primitives (crash-durable JSONL).

See :mod:`mote.runtime.ledger.append_ledger`. Domain ledgers (EXTERNAL
tool-effect idempotency, hunk change-attribution) subclass
:class:`AppendOnlyLedger`.
"""

from __future__ import annotations

from mote.runtime.ledger.append_ledger import AppendOnlyLedger, LedgerRecord
from mote.runtime.ledger.run_journal import (
    COMPLETED,
    FAILED,
    JOURNAL_FILE_NAME,
    KIND_THINK,
    KIND_TIMER,
    KIND_TOOL,
    STARTED,
    RunJournal,
    StepRecord,
    run_journaled_step,
)

__all__ = [
    "AppendOnlyLedger",
    "LedgerRecord",
    "RunJournal",
    "StepRecord",
    "run_journaled_step",
    "STARTED",
    "COMPLETED",
    "FAILED",
    "KIND_THINK",
    "KIND_TOOL",
    "KIND_TIMER",
    "JOURNAL_FILE_NAME",
]
