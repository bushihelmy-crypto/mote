from __future__ import annotations

import pytest

from mote.contracts.execution.models import ExecutionOperationContext, MutationStatus
from mote.contracts.output import AcceptedOutput, CommittedOutput
from mote.kernel.commands.contracts import HistoryProjection
from mote.runtime.persistence.execution_transaction import RuntimeExecutionTransaction


class EmptyOutputEngine:
    staged_output = None


class Memory:
    def __init__(self):
        self.messages = []

    async def add_batch(self, messages):
        self.messages.extend(messages)


class Checkpoint:
    def __init__(self):
        self.recorded = 0
        self.discarded = 0

    def record_result(self):
        self.recorded += 1

    def discard(self):
        self.discarded += 1


def context(operation_id, *, fence=7, revision=None):
    return ExecutionOperationContext("run", "attempt", operation_id, fence, revision)


@pytest.mark.asyncio
async def test_projection_mutation_is_durable_idempotent_and_fenced():
    memory = Memory()
    checkpoint = Checkpoint()
    drains = []

    async def drain():
        drains.append(True)

    transaction = RuntimeExecutionTransaction(
        run_id="run",
        fencing_token=7,
        memory=memory,
        output_engine=EmptyOutputEngine(),
        inference_checkpoint=checkpoint,
        drain_writes=drain,
    )
    projection = HistoryProjection(("message",), "fingerprint")
    applied = await transaction.record_model_turn(context("turn"), projection)
    repeated = await transaction.record_model_turn(context("turn"), projection)
    fenced = await transaction.record_model_turn(context("late", fence=6), projection)

    assert applied.status is MutationStatus.APPLIED
    assert repeated.status is MutationStatus.ALREADY_APPLIED
    assert fenced.status is MutationStatus.FENCED
    assert memory.messages == ["message"]
    assert checkpoint.recorded == 1
    assert len(drains) == 1


@pytest.mark.asyncio
async def test_staged_output_is_immutable_and_commit_reuses_exact_record():
    accepted = AcceptedOutput("candidate", "contract", "1", "schema", {"ok": True})
    committed = CommittedOutput("candidate", "contract", "schema", {"ok": True})

    class OutputEngine:
        staged_output = accepted

        async def commit(self):
            return committed

    checkpoint = Checkpoint()

    async def drain():
        return None

    transaction = RuntimeExecutionTransaction(
        run_id="run",
        fencing_token=7,
        memory=Memory(),
        output_engine=OutputEngine(),
        inference_checkpoint=checkpoint,
        drain_writes=drain,
    )
    history = HistoryProjection(("accepted",), "accepted-history")
    staged = await transaction.stage_accepted_output(context("stage"), accepted, history)
    conflict = await transaction.stage_accepted_output(
        context("other-stage"),
        AcceptedOutput("other", "contract", "1", "schema", {"ok": False}),
        history,
    )
    result = await transaction.commit_terminal_output(context("commit"), "candidate")

    assert staged.status is MutationStatus.APPLIED
    assert conflict.status is MutationStatus.CONFLICT
    assert result is committed
    assert checkpoint.discarded == 1
    assert (await transaction.recover_frontier("run")).terminal_committed is True
