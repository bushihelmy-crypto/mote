from __future__ import annotations

import asyncio
from pathlib import Path

from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.runtime.inference.epochs import ExecutionEpochAuthority
from mote.ztest.inference.test_generation import _artifact

ROOT = Path(__file__).resolve().parents[2]


def test_epoch_authority_advances_each_dimension_without_torn_pairs() -> None:
    authority = ExecutionEpochAuthority(backup_epoch=3, admission_epoch=7)

    assert authority.snapshot().pair() == (3, 7)
    assert authority.advance_backup().pair() == (4, 7)
    assert authority.advance_admission().pair() == (4, 8)
    assert authority.pair() == (4, 8)


def test_production_inference_composition_has_no_constant_zero_epoch_provider() -> None:
    offenders = []
    for root in (ROOT / "runtime", ROOT / "product"):
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "epoch_provider=lambda: (0, 0)" in source or "epoch = lambda: (0, 0)" in source:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_sqlite_epoch_authority_survives_activation_backup_and_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "authority.sqlite3"
        store = SQLiteAttemptReceiptStore(path)
        await store.initialize()
        artifact = _artifact("generation-a")
        await store.stage_generation(artifact)
        await store.activate_generation(artifact.generation_id, artifact.artifact_digest)
        assert (await store.execution_epoch_snapshot()).pair() == (1, 2)
        assert (await store.advance_backup_epoch()).pair() == (2, 2)
        reopened = SQLiteAttemptReceiptStore(path)
        await reopened.initialize()
        assert (await reopened.execution_epoch_snapshot()).pair() == (2, 2)

    asyncio.run(scenario())
