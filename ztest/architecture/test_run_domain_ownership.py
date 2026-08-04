from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generic_run_journal_production_surface_is_absent() -> None:
    for path in (
        "runtime/ledger/run_journal.py",
        "runtime/durable/backend.py",
        "runtime/durable/factory.py",
        "runtime/durable/inference_journal.py",
        "runtime/durable/temporal/_backend.py",
    ):
        assert not (ROOT / path).exists()

    forbidden = ("RunJournal", "StepRecord", "JsonlBackend", "InferenceJournal", "run_journal")
    for directory in ("contracts", "kernel", "runtime", "orchestration", "product"):
        for path in (ROOT / directory).rglob("*.py"):
            if path.is_relative_to(ROOT / "product/migrations"):
                continue
            source = path.read_text(encoding="utf-8")
            assert not any(symbol in source for symbol in forbidden), path


def test_run_domains_have_distinct_canonical_owners() -> None:
    assert (ROOT / "runtime/tools/effect_store.py").exists()
    assert (ROOT / "runtime/models/failover/model_journal.py").exists()
    assert (ROOT / "runtime/session/timers.py").exists()

    executor = (ROOT / "runtime/tools/tool_executor.py").read_text(encoding="utf-8")
    capabilities = (ROOT / "runtime/agent/capabilities.py").read_text(encoding="utf-8")
    temporal = (ROOT / "product/workflows/temporal_effects.py").read_text(encoding="utf-8")
    assert "ToolEffectStore" in executor
    assert "SessionTimerStore" in capabilities
    assert "RunJournal" not in temporal
