"""Executable reference SLOs for control-plane and recovery hot paths."""

from __future__ import annotations

import json
import time

import pytest

from mote.kernel.flow.engine import RUN_EVENT_BUFFER_SIZE
from mote.kernel.flow.graph import AgentGraph, EffectKind, End, GraphRunner, NodeId, Transition
from mote.kernel.flow.slo import DEFAULT_RUNTIME_SLO
from mote.runtime.disk import Journal
from mote.runtime.ledger import RunJournal
from mote.runtime.workspace import ArtifactKind, WorkspaceStore


def test_run_journal_rebuild_meets_reference_recovery_slo(tmp_path):
    slo = DEFAULT_RUNTIME_SLO
    store = WorkspaceStore(root=str(tmp_path))
    path = store.space("slo", ArtifactKind.LEDGER) / "effects.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for seq in range(slo.recovery_records):
        records.append(
            json.dumps(
                {
                    "step_id": f"think:{seq}",
                    "kind": "think",
                    "effect": "pure",
                    "status": "completed",
                    "seq": seq,
                    "payload": "{}",
                }
            )
        )
    path.write_text("\n".join(records) + "\n", encoding="utf-8")

    started = time.perf_counter()
    journal = RunJournal("slo", store=store)
    elapsed = time.perf_counter() - started

    assert len(list(journal.records())) == slo.recovery_records
    assert elapsed < slo.recovery_seconds


@pytest.mark.asyncio
async def test_graph_dispatch_meets_reference_control_plane_slo():
    slo = DEFAULT_RUNTIME_SLO

    class CounterNode:
        node_id = NodeId.BUDGET
        effect_kind = EffectKind.PURE
        allowed_targets = frozenset({NodeId.BUDGET})

        async def run(self, state):
            state[0] += 1
            if state[0] == slo.graph_transitions:
                return End(state[0])
            return Transition(NodeId.BUDGET)

    graph = AgentGraph(start=NodeId.BUDGET, nodes={NodeId.BUDGET: CounterNode()})
    started = time.perf_counter()
    result = await GraphRunner(graph, max_steps=slo.graph_transitions).run([0])
    elapsed = time.perf_counter() - started

    assert result == slo.graph_transitions
    assert elapsed < slo.graph_seconds


@pytest.mark.asyncio
async def test_disk_barrier_meets_reference_batch_slo(tmp_path):
    slo = DEFAULT_RUNTIME_SLO
    journal = Journal(tmp_path / "barrier.jsonl")
    for seq in range(slo.disk_barrier_records):
        journal.append_line(str(seq))

    started = time.perf_counter()
    await journal.writer.drain()
    elapsed = time.perf_counter() - started

    assert sum(1 for _ in journal.iter_raw_lines()) == slo.disk_barrier_records
    assert elapsed < slo.disk_barrier_seconds


def test_public_event_buffer_is_bounded_by_slo():
    assert RUN_EVENT_BUFFER_SIZE == DEFAULT_RUNTIME_SLO.run_event_buffer
