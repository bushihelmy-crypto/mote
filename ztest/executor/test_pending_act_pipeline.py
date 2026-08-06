from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_receipt_reconciler_has_no_dispatch_or_executor_capability() -> None:
    source = (ROOT / "runtime/session/execution_reconciliation.py").read_text()
    assert "ToolExecutor" not in source
    assert ".dispatch(" not in source
    assert "query_external_effect_result" in source


def test_pending_act_terminal_batch_owns_receipt_result_and_cursor() -> None:
    source = (ROOT / "runtime/session/pending_act.py").read_text()
    for fact in (
        "ExternalEffectFinishedEvent",
        "PendingActionResultCommittedEvent",
        "PendingActSettledEvent",
        "RunRecoveryCursorAdvancedEvent",
    ):
        assert fact in source
