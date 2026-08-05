from __future__ import annotations

import pytest

from mote.contracts.conversation import AIMessage
from mote.contracts.execution.models import ExecutionOperationContext, MutationStatus
from mote.contracts.output import CommittedOutput, ValidatedCandidate
from mote.kernel.commands.contracts import HistoryProjection
from mote.runtime.persistence.execution_transaction import RuntimeExecutionTransaction


class EmptyOutputEngine:
    validated_candidate = None


class Memory:
    def __init__(self):
        self.messages = []

    async def add_batch(self, messages):
        self.messages.extend(messages)

    def apply_committed_messages(self, messages):
        self.messages.extend(messages)


class Checkpoint:
    def __init__(self):
        self.recorded = 0
        self.discarded = 0

    async def record_result(self):
        self.recorded += 1

    def discard(self):
        self.discarded += 1

    async def prepare_consumption(self, operation_id):
        from mote.contracts.events.model import InferenceCheckpointConsumedEvent

        self.recorded += 1
        return InferenceCheckpointConsumedEvent("model-call", "attempt", 1, operation_id)

    def acknowledge_consumption(self, event):
        self.discarded += 1


class FactSink:
    def __init__(self):
        self.batches = []

    async def commit_facts(self, events):
        self.batches.append(events)

    async def commit_fact(self, event):
        await self.commit_facts((event,))


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
        session_fact_sink=FactSink(),
        drain_writes=drain,
    )
    projection = HistoryProjection(("message",), "fingerprint")
    applied = await transaction.record_effect_intent(context("turn"), projection)
    repeated = await transaction.record_effect_intent(context("turn"), projection)
    fenced = await transaction.record_effect_intent(context("late", fence=6), projection)

    assert applied.status is MutationStatus.APPLIED
    assert repeated.status is MutationStatus.ALREADY_APPLIED
    assert fenced.status is MutationStatus.FENCED
    assert memory.messages == ["message"]
    assert checkpoint.recorded == 1
    assert len(drains) == 0


@pytest.mark.asyncio
async def test_final_output_is_immutable_and_commit_reuses_exact_record():
    validated = ValidatedCandidate("candidate", "contract", "schema", {"ok": True}, {"ok": True})
    committed = CommittedOutput("candidate", "contract", "schema", {"ok": True})
    session_facts = FactSink()

    class OutputEngine:
        validated_candidate = validated

        async def commit_final(self, message, *, companion_facts=(), fact_sink=None):
            assert len(companion_facts) == 1
            assert fact_sink is session_facts
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
        session_fact_sink=session_facts,
        drain_writes=drain,
    )
    result = await transaction.commit_final_output(context("commit"), validated, AIMessage(content="done"))
    repeated = await transaction.commit_final_output(context("commit"), validated, AIMessage(content="done"))

    assert result is committed
    assert repeated is committed
    assert checkpoint.discarded == 1
    assert (await transaction.recover_frontier("run")).terminal_committed is True
