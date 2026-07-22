#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentControl — the multi-agent control plane."""

import asyncio
import types

import pytest

from mote.common.interface.event_subscriber import ObservationSubscriber, SyncObserver
from mote.common.schema.messages import UserMessage
from mote.common.schema.queue import MessageQueue
from mote.environment.agent_path import AgentPath
from mote.environment.control import AgentControl, format_completion_notification
from mote.environment.exceptions import AgentLimitReached, AgentNotFound, AgentNotKnown
from mote.environment.mailbox import DeliveryMode, InterAgentCommunication
from mote.environment.registry import AgentMetadata
from mote.environment.runtime import AgentRuntime, AgentStatus
from mote.environment.store import ResidencyStore


class FakeRole:
    def __init__(self, session_id):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())
        self.observed_turns = []

    @property
    def session_id(self):
        return self._session_id

    async def run(self, with_message=None):
        drained = self.state.msg_buffer.pop_all()
        self.observed_turns.append([m.content for m in drained])
        return "ok"

    def dump(self):
        return {"session_id": self._session_id}


def fake_role_loader(role_dump):
    return FakeRole(role_dump.get("session_id", "?"))


def make_runtime(session_id, *, status=AgentStatus.IDLE):
    rt = AgentRuntime(FakeRole(session_id))
    rt.status = status
    return rt


def make_control(tmp_path, **kwargs):
    return AgentControl(
        store=ResidencyStore(base_dir=str(tmp_path)),
        role_loader=fake_role_loader,
        **kwargs,
    )


@pytest.fixture
def control(tmp_path):
    return make_control(tmp_path)


def test_add_agent_registers_in_map_and_scheduler(control):
    rt = make_runtime("a")
    control.add_agent(rt, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/a")))
    assert control.get_runtime("a") is rt
    assert control.scheduler.get_runtime("a") is rt
    assert control.registry.agent_id_for_path(AgentPath.from_string("/root/a")) == "a"


def test_register_session_root(control):
    control.register_session_root("root-1", None)
    assert control.registry.agent_id_for_path(AgentPath.root()) == "root-1"


def test_get_status_known_and_unknown(control):
    rt = make_runtime("a", status=AgentStatus.RUNNING)
    control.add_agent(rt)
    assert control.get_status("a") == AgentStatus.RUNNING
    assert control.get_status("ghost") == AgentStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_send_input_trigger_runs_turn(control):
    rt = make_runtime("a")
    control.add_agent(rt)
    control.send_input("a", UserMessage("hello"))
    turns = await control.run(1)
    assert turns == 1
    assert rt.role.observed_turns == [["hello"]]


@pytest.mark.asyncio
async def test_send_input_queue_only_defers(control):
    rt = make_runtime("a")
    control.add_agent(rt)
    control.send_input("a", UserMessage("later"), mode=DeliveryMode.QUEUE_ONLY)
    turns = await control.run(1)
    assert turns == 0
    assert not rt.mailbox.empty()


def test_send_input_unknown_agent_raises(control):
    with pytest.raises(AgentNotFound):
        control.send_input("ghost", UserMessage("x"))


def test_send_input_parks_when_execution_cap_exhausted(tmp_path):
    # A trigger-turn delivery that arrives while the execution cap is exhausted
    # is parked (never raises, never drops): the mailbox stays empty until a
    # flush fulfils it once capacity frees up.
    control = make_control(tmp_path, max_agents=1)
    rt = make_runtime("a")
    control.add_agent(rt)
    guard = control.limiter.guard()  # occupy the single execution slot
    result = control.send_input("a", UserMessage("x"))
    assert result is rt  # target was loaded (parked, not refused)
    assert rt.mailbox.empty()  # nothing delivered yet — parked behind the cap
    assert control._pending.has_pending("a")
    guard.release()  # capacity freed
    control.send_input("a", UserMessage("y"))  # now delivers immediately
    assert not rt.mailbox.empty()


@pytest.mark.asyncio
async def test_parked_execution_capped_delivery_flushed(tmp_path):
    # The parked item from an exhausted execution cap is fulfilled by a flush
    # once the slot frees.
    control = make_control(tmp_path, max_agents=1)
    rt = make_runtime("a")
    control.add_agent(rt)
    guard = control.limiter.guard()
    control.send_input("a", UserMessage("x"))
    assert rt.mailbox.empty()
    guard.release()
    flushed = await control._flush_pending_deliveries()
    assert flushed == 1
    assert not rt.mailbox.empty()
    assert not control._pending.has_pending("a")


def test_send_communication_records_last_task_message(control):
    rt = make_runtime("a")
    control.add_agent(rt, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/a")))
    comm = InterAgentCommunication.new(
        author=AgentPath.root(),
        recipient=AgentPath.from_string("/root/a"),
        content="do the thing",
        trigger_turn=True,
    )
    control.send_inter_agent_communication("a", comm)
    meta = control.registry.agent_metadata_for_id("a")
    assert meta.last_task_message == "do the thing"
    assert rt.mailbox.has_trigger_turn()


def test_resolve_reference_by_session_id(control):
    rt = make_runtime("sess-xyz")
    control.add_agent(rt)
    assert control.resolve_agent_reference("sess-xyz") == "sess-xyz"


def test_resolve_reference_by_path(control):
    rt = make_runtime("a")
    control.add_agent(rt, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/researcher")))
    assert control.resolve_agent_reference("/root/researcher") == "a"
    # relative to root
    assert control.resolve_agent_reference("researcher") == "a"


def test_resolve_reference_by_nickname(control):
    rt = make_runtime("a")
    control.add_agent(
        rt,
        metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/a"), agent_nickname="Plato"),
    )
    assert control.resolve_agent_reference("Plato") == "a"


def test_resolve_reference_unknown_raises(control):
    with pytest.raises(AgentNotKnown):
        control.resolve_agent_reference("nobody")


@pytest.mark.asyncio
async def test_rehydrate_on_send_to_evicted_agent(control):
    # Materialize an agent to disk without keeping it live.
    role = FakeRole("evicted")
    role.state.msg_buffer.push(UserMessage("old-buffered"))
    rt = AgentRuntime(role)
    await control.store.materialize(rt)
    assert control.get_runtime("evicted") is None

    # Sending to it rehydrates from disk.
    control.send_input("evicted", UserMessage("wake up"))
    restored = control.get_runtime("evicted")
    assert restored is not None
    assert not control.store.has("evicted")  # forgotten after load
    turns = await control.run(1)
    assert turns == 1
    # both the restored buffer and the new message were delivered
    assert restored.role.observed_turns == [["old-buffered", "wake up"]]


@pytest.mark.asyncio
async def test_completion_watcher_notifies_parent(control):
    parent = make_runtime("parent")
    child = make_runtime("child", status=AgentStatus.RUNNING)
    control.add_agent(parent, metadata=AgentMetadata(agent_path=AgentPath.root()))
    control.add_agent(child, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/child")))

    task = control.start_completion_watcher(
        "child",
        "parent",
        child_path=AgentPath.from_string("/root/child"),
        parent_path=AgentPath.root(),
    )
    # child finishes
    child.status = AgentStatus.COMPLETED
    await task
    # parent received a queue-only notification (no trigger)
    assert not parent.mailbox.empty()
    assert not parent.mailbox.has_trigger_turn()
    drained = parent.mailbox.drain_for_turn()
    assert "finished with status" in drained[0].content


@pytest.mark.asyncio
async def test_interrupt_unknown_agent(control):
    assert await control.interrupt("ghost") == AgentStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_interrupt_idle_agent_marks_interrupted(control):
    rt = make_runtime("a", status=AgentStatus.RUNNING)
    control.add_agent(rt)
    status = await control.interrupt("a")
    assert status == AgentStatus.INTERRUPTED


def test_format_completion_notification():
    msg = format_completion_notification("researcher", AgentStatus.COMPLETED)
    assert "researcher" in msg
    assert "completed" in msg


# ---------------------------------------------------------------------------
# Runtime-level bus: agent-lifecycle milestones
# ---------------------------------------------------------------------------


class _CaptureSub(ObservationSubscriber, SyncObserver):
    """A sync subscriber that records agent-lifecycle events off the runtime bus."""

    priority = 50

    def __init__(self):
        self.events = []

    async def handle(self, event) -> None:
        return None

    def handle_sync(self, event) -> None:
        from mote.common.events import AgentLifecycleEvent

        if isinstance(event, AgentLifecycleEvent):
            self.events.append((event.phase, event.session_id))


def test_runtime_bus_emits_added_on_add_agent(control):
    cap = _CaptureSub()
    control.event_bus.subscribe(cap)
    control.add_agent(make_runtime("a"))
    assert ("added", "a") in cap.events


@pytest.mark.asyncio
async def test_runtime_bus_emits_interrupted(control):
    cap = _CaptureSub()
    control.event_bus.subscribe(cap)
    control.add_agent(make_runtime("a", status=AgentStatus.RUNNING))
    await control.interrupt("a")
    assert ("interrupted", "a") in cap.events


# ---------------------------------------------------------------------------
# Spawn authority: AgentControl.spawn_agent (the single birth channel)
# ---------------------------------------------------------------------------
from mote.common.agent_control import ContextPolicy, Lifecycle, SpawnSpec, current_control  # noqa: E402


class SpawnRole:
    """A FakeRole whose session_id is unique per construction, with a summary."""

    _counter = 0

    def __init__(self, *, summary="result"):
        SpawnRole._counter += 1
        self._session_id = f"spawned-{SpawnRole._counter}"
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())
        from mote.common.schema.output import CommittedOutput, RunResult, TranscriptRef

        committed = CommittedOutput("candidate", "mote.text@1", "sha", summary)
        self.run_result = RunResult(
            output=summary,
            output_record=committed,
            transcript=TranscriptRef(session_id=self._session_id),
        )
        self.cleaned = False

    @property
    def session_id(self):
        return self._session_id

    async def run(self, with_message=None):
        return self.run_result

    async def cleanup(self):
        self.cleaned = True

    def dump(self):
        return {"session_id": self._session_id}


def _spec(nickname="worker", lifecycle=Lifecycle.EPHEMERAL, parent_id=None, **kw):
    return SpawnSpec(
        role_factory=lambda ctx: SpawnRole(),
        nickname=nickname,
        lifecycle=lifecycle,
        parent_id=parent_id,
        **kw,
    )


@pytest.mark.asyncio
async def test_spawn_agent_registers_path_and_nickname(control):
    handle = await control.spawn_agent(_spec(nickname="Researcher"))
    assert handle.agent_path.as_str().startswith("/root/researcher_")
    meta = control.registry.agent_metadata_for_id(handle.session_id)
    assert meta is not None
    assert meta.agent_path == handle.agent_path


@pytest.mark.asyncio
async def test_spawn_agent_ephemeral_not_in_scheduler_but_occupies_cap(tmp_path):
    control = make_control(tmp_path, max_agents=2)
    h1 = await control.spawn_agent(_spec())
    # EPHEMERAL children are run inline by the caller, not the scheduler.
    assert control.scheduler.get_runtime(h1.session_id) is None
    # ...but they still occupy a registry slot.
    h2 = await control.spawn_agent(_spec())
    with pytest.raises(AgentLimitReached):
        await control.spawn_agent(_spec())
    # releasing one frees the cap again
    await h1.aclose()
    h3 = await control.spawn_agent(_spec())
    assert h3 is not None


@pytest.mark.asyncio
async def test_spawn_agent_managed_enters_scheduler(control):
    handle = await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED))
    assert control.scheduler.get_runtime(handle.session_id) is handle.runtime


@pytest.mark.asyncio
async def test_spawn_agent_factory_failure_rolls_back(tmp_path):
    control = make_control(tmp_path, max_agents=1)

    def boom(ctx):
        raise RuntimeError("factory exploded")

    spec = SpawnSpec(role_factory=boom, nickname="bad")
    with pytest.raises(RuntimeError):
        await control.spawn_agent(spec)
    # both the residency slot and the registry count rolled back → fresh spawn ok
    assert control.residency.pending_slots == 0
    ok = await control.spawn_agent(_spec())
    assert ok is not None


# ---------------------------------------------------------------------------
# Workflow B: cap = live incarnation (residency-enforced, identity persists)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_spawn_cap_enforced_by_residency(tmp_path):
    # cap=2; two MANAGED residents that are IDLE (non-final → not evictable),
    # so the third spawn finds no room and is refused.
    control = make_control(tmp_path, max_agents=2)
    await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED))
    await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED))
    with pytest.raises(AgentLimitReached):
        await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED))


@pytest.mark.asyncio
async def test_ephemeral_aclose_frees_live_slot(tmp_path):
    control = make_control(tmp_path, max_agents=2)
    h1 = await control.spawn_agent(_spec())
    await control.spawn_agent(_spec())
    with pytest.raises(AgentLimitReached):
        await control.spawn_agent(_spec())
    # closing an ephemeral handle releases its (pending) live slot
    await h1.aclose()
    assert control.residency.pending_slots == 1
    h3 = await control.spawn_agent(_spec())
    assert h3 is not None


@pytest.mark.asyncio
async def test_managed_eviction_frees_slot_but_keeps_identity(tmp_path):
    control = make_control(tmp_path, max_agents=1)
    h1 = await control.spawn_agent(_spec(nickname="first", lifecycle=Lifecycle.MANAGED))
    # make it unloadable: final status + empty mailbox/buffer (idle leaf)
    h1.runtime.status = AgentStatus.COMPLETED
    # a second MANAGED spawn evicts the LRU resident (h1) to free a slot
    h2 = await control.spawn_agent(_spec(nickname="second", lifecycle=Lifecycle.MANAGED))
    # h1 dropped from the live map + materialized to disk...
    assert control.get_runtime(h1.session_id) is None
    assert control.store.has(h1.session_id)
    # ...but its identity persists in the registry (driving cap == LIVE incarnations)
    live_ids = {m.agent_id for m in control.registry.live_agents()}
    assert h1.session_id in live_ids
    assert h2.session_id in live_ids


@pytest.mark.asyncio
async def test_rehydrate_at_hard_cap_parks_then_back_pressures(tmp_path):
    # cap=1; one busy (RUNNING, non-evictable) resident + one evicted-to-disk
    # agent. A sync delivery to the evicted agent can't make room → it PARKS
    # (never raises, never drops) and stays parked while the resident is busy.
    control = make_control(tmp_path, max_agents=1)
    # Evict an agent to disk directly.
    role = FakeRole("evicted")
    rt = AgentRuntime(role)
    await control.store.materialize(rt)
    # Occupy the single live slot with a busy resident.
    busy = make_runtime("busy", status=AgentStatus.RUNNING)
    control.add_agent(busy)
    # Synchronous delivery cannot make room → parks rather than raising.
    result = control.send_input("evicted", UserMessage("wake"))
    assert result is None
    assert control.get_runtime("evicted") is None
    assert control._pending.has_pending("evicted")
    # An async fulfilment pass also can't evict the busy resident → back-pressure:
    # the delivery stays parked (never lost), to be retried next boundary.
    flushed = await control._flush_pending_deliveries()
    assert flushed == 0
    assert control.get_runtime("evicted") is None
    assert control._pending.has_pending("evicted")


@pytest.mark.asyncio
async def test_sustained_back_pressure_emits_lifecycle_event(tmp_path):
    # A target stuck parked behind a permanently-busy resident: the first few
    # flush passes are silent churn, but once the count crosses the stuck
    # threshold a single AgentLifecycleEvent(phase="delivery_back_pressure") is
    # surfaced (pure observability — delivery semantics are unchanged: it stays
    # parked the whole time).
    from mote.environment.control import _DELIVERY_STUCK_FLUSHES

    control = make_control(tmp_path, max_agents=1)
    await control.store.materialize(AgentRuntime(FakeRole("evicted")))
    busy = make_runtime("busy", status=AgentStatus.RUNNING)  # never evictable
    control.add_agent(busy)
    cap = _CaptureSub()
    control.event_bus.subscribe(cap)
    assert control.send_input("evicted", UserMessage("wake")) is None

    # Below the threshold: parked, but no event yet.
    for _ in range(_DELIVERY_STUCK_FLUSHES - 1):
        assert await control._flush_pending_deliveries() == 0
    assert ("delivery_back_pressure", "evicted") not in cap.events

    # The pass that crosses the threshold emits exactly once.
    assert await control._flush_pending_deliveries() == 0
    assert ("delivery_back_pressure", "evicted") in cap.events
    assert cap.events.count(("delivery_back_pressure", "evicted")) == 1
    # The message was never lost — it is still parked under back-pressure.
    assert control._pending.has_pending("evicted")


@pytest.mark.asyncio
async def test_back_pressure_event_resets_after_delivery(tmp_path):
    # Once a delivery is finally fulfilled the stuck counter resets, so a later
    # parked delivery starts its own silent grace period again.
    from mote.environment.control import _DELIVERY_STUCK_FLUSHES

    control = make_control(tmp_path, max_agents=1)
    await control.store.materialize(AgentRuntime(FakeRole("evicted")))
    busy = make_runtime("busy", status=AgentStatus.RUNNING)
    control.add_agent(busy)
    assert control.send_input("evicted", UserMessage("wake")) is None
    for _ in range(_DELIVERY_STUCK_FLUSHES):
        await control._flush_pending_deliveries()
    # Resident becomes idle → next flush delivers and clears the counter.
    busy.status = AgentStatus.COMPLETED
    assert await control._flush_pending_deliveries() == 1
    assert not control._pending.has_pending("evicted")
    # A fresh stuck cycle counts from zero again (no carryover).
    assert control._pending.note_back_pressure("evicted") == 1


@pytest.mark.asyncio
async def test_rehydrate_at_hard_cap_fulfilled_when_resident_idle(tmp_path):
    # Same setup, but once the resident becomes evictable (final + idle) the
    # next flush awaits an LRU eviction, rehydrates the target, and delivers.
    control = make_control(tmp_path, max_agents=1)
    role = FakeRole("evicted")
    await control.store.materialize(AgentRuntime(role))
    busy = make_runtime("busy", status=AgentStatus.RUNNING)
    control.add_agent(busy)
    # Park the delivery behind the busy resident.
    assert control.send_input("evicted", UserMessage("wake")) is None
    assert control._pending.has_pending("evicted")
    # The resident finishes its work → becomes an evictable idle leaf.
    busy.status = AgentStatus.COMPLETED
    # Now the async fulfilment evicts it, rehydrates the target, and delivers.
    flushed = await control._flush_pending_deliveries()
    assert flushed == 1
    rehydrated = control.get_runtime("evicted")
    assert rehydrated is not None
    assert not rehydrated.mailbox.empty()
    assert not control._pending.has_pending("evicted")
    # the busy resident was evicted to disk to free the single live slot
    assert control.get_runtime("busy") is None
    assert control.store.has("busy")


@pytest.mark.asyncio
async def test_parked_delivery_fulfilled_by_run_boundary_flush(tmp_path):
    # End-to-end: a delivery parked behind a busy resident is fulfilled by the
    # scheduler's per-round boundary flush in run(k) — no direct flush call.
    control = make_control(tmp_path, max_agents=1)
    role = FakeRole("evicted")
    await control.store.materialize(AgentRuntime(role))
    # An idle (final) resident occupies the slot; it is evictable but no flush
    # has run yet, so the delivery parks on the synchronous path.
    resident = make_runtime("resident", status=AgentStatus.COMPLETED)
    control.add_agent(resident)
    assert control.send_input("evicted", UserMessage("wake")) is None
    assert control._pending.has_pending("evicted")
    # run(k) flushes at the start of each round: it evicts the idle resident,
    # rehydrates the target, delivers the parked message, then runs its turn.
    turns = await control.run(2)
    assert turns >= 1
    assert not control._pending.has_pending("evicted")
    assert control.get_runtime("evicted") is not None
    assert control.get_runtime("evicted").role.observed_turns == [["wake"]]


@pytest.mark.asyncio
async def test_release_child_clears_parked_deliveries(tmp_path):
    # A delivery parked for an agent is forgotten once that agent is released:
    # the target is gone for good, so its parked mail must not linger.
    control = make_control(tmp_path, max_agents=1)
    role = FakeRole("evicted")
    await control.store.materialize(AgentRuntime(role))
    busy = make_runtime("busy", status=AgentStatus.RUNNING)
    control.add_agent(busy)
    control.send_input("evicted", UserMessage("wake"))
    assert control._pending.has_pending("evicted")
    control.release_child("evicted")
    assert not control._pending.has_pending("evicted")


@pytest.mark.asyncio
async def test_parked_trigger_keeps_fleet_non_quiescent(tmp_path):
    # A trigger-turn delivery parked behind a busy resident is outstanding work:
    # the fleet must not report quiescent until it is fulfilled.
    control = make_control(tmp_path, max_agents=1)
    role = FakeRole("evicted")
    await control.store.materialize(AgentRuntime(role))
    busy = make_runtime("busy", status=AgentStatus.RUNNING)
    control.add_agent(busy)
    control.send_input("evicted", UserMessage("wake"))
    assert control._pending.has_pending("evicted")
    # parked trigger work → not quiescent even though no runtime is woken
    assert control.quiescent() is False
    # once it's released (target gone), the parked work clears → quiescent
    control.release_child("evicted")
    assert control.quiescent() is True


@pytest.mark.asyncio
async def test_spawn_agent_depth_limit(tmp_path):
    control = make_control(tmp_path, max_depth=1)
    # depth-1 child off root is allowed
    child = await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED))
    # a grandchild (depth 2) off that child exceeds max_depth=1
    with pytest.raises(AgentLimitReached):
        await control.spawn_agent(_spec(parent_id=child.session_id))


@pytest.mark.asyncio
async def test_spawn_agent_run_to_completion_releases(control):
    handle = await control.spawn_agent(_spec())
    out = await handle.run_to_completion(UserMessage("go"))
    assert out.output == "result"
    # released from registry after the inline run
    assert control.registry.agent_metadata_for_id(handle.session_id) is None


@pytest.mark.asyncio
async def test_managed_ttl_watchdog_interrupts_at_deadline(control):
    # A MANAGED child with a short TTL is interrupted by the watchdog once the
    # wall-clock budget elapses (reusing the existing interrupt() path).
    handle = await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED, timeout_seconds=0.02))
    # It starts non-final (freshly scheduled); let the TTL watchdog fire.
    await asyncio.sleep(0.08)
    assert control.get_status(handle.session_id) == AgentStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_managed_no_timeout_registers_no_watchdog(control):
    # Without timeout_seconds no TTL watchdog is scheduled → the child is left
    # to the scheduler and never auto-interrupted.
    before = len(control._watchers)
    handle = await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED))
    # completion watcher is only started when parent_id is set (it is not here),
    # so no watcher tasks should have been added at all.
    assert len(control._watchers) == before
    assert control.get_status(handle.session_id) != AgentStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_managed_ttl_watchdog_noops_when_child_finishes_early(control):
    # A child that reaches a final status before its deadline is dropped from
    # _runtimes on release; the watchdog's post-sleep lookup then no-ops (no
    # crash, no spurious interrupt of an unrelated later agent).
    handle = await control.spawn_agent(_spec(lifecycle=Lifecycle.MANAGED, timeout_seconds=0.05))
    control.get_runtime(handle.session_id).status = AgentStatus.COMPLETED
    control.release_child(handle.session_id)  # gone from _runtimes
    await asyncio.sleep(0.09)  # outlast the TTL; watchdog must not raise
    # nothing to assert beyond "no exception" — the child is already released.
    assert control.get_runtime(handle.session_id) is None


def _cost_role(summary="result"):
    from mote.router.cost import CostTracker

    role = SpawnRole(summary=summary)
    role._context = types.SimpleNamespace(cost_manager=CostTracker(), config=None)
    role._config = None
    return role


@pytest.mark.asyncio
async def test_spawn_agent_builds_cost_node_under_parent(control):
    # Root parent carrying its own tracker -> seeds the cost tree root.
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)
    assert control.cost_root is not None
    assert control.cost_root.tracker is parent_role._context.cost_manager

    child_role = _cost_role()
    spec = SpawnSpec(
        role_factory=lambda ctx: child_role,
        nickname="worker",
        parent_id=parent_rt.session_id,
    )
    handle = await control.spawn_agent(spec)
    node = control.cost_node_for(handle.session_id)
    assert node is not None
    # Child keeps its OWN tracker (per-node attribution), parent pointer correct.
    assert node.tracker is child_role._context.cost_manager
    assert node.parent is control.cost_root
    assert node in control.cost_root.children


@pytest.mark.asyncio
async def test_child_usage_only_in_own_bucket_but_subtree_includes_it(control):
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)

    child_role = _cost_role()
    handle = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: child_role,
            nickname="worker",
            parent_id=parent_rt.session_id,
        )
    )
    # Child records 1000 tokens against a known model; parent records nothing.
    from mote.router.cost import TokenUsage

    child_node = control.cost_node_for(handle.session_id)
    child_node.tracker.add(TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500), "gpt-4o")
    # Parent's own bucket stays empty; the fleet subtree total includes the child.
    assert control.cost_root.tracker.total_cost == 0.0
    assert control.cost_root.subtree_cost() == child_node.tracker.total_cost
    assert control.cost_root.subtree_cost() > 0.0


@pytest.mark.asyncio
async def test_skill_fork_shared_tracker_not_double_counted(control):
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)
    shared = parent_role._context.cost_manager

    # A skill_fork child declares SHARE_PARENT: the authority hands it the
    # parent's own context (the factory never touches context), so the shared
    # tracker is deduped in the cost tree.
    child_role = SpawnRole()
    handle = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: child_role,
            nickname="forked",
            parent_id=parent_rt.session_id,
            context_policy=ContextPolicy.SHARE_PARENT,
        )
    )
    # The authority shared the parent's context onto the child.
    assert child_role._context is parent_role._context
    # No separate node is created for the shared bucket.
    assert control.cost_node_for(handle.session_id) is None
    from mote.router.cost import TokenUsage

    shared.add(TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500), "gpt-4o")
    # Counted exactly once (root self == subtree).
    assert control.cost_root.subtree_cost() == shared.total_cost


@pytest.mark.asyncio
async def test_subtree_estimated_flag_rolls_up(control):
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)
    child_role = _cost_role()
    handle = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: child_role,
            nickname="worker",
            parent_id=parent_rt.session_id,
        )
    )
    from mote.router.cost import TokenUsage

    # Unknown model -> has_unknown_model_cost on the child, rolled up to root.
    control.cost_node_for(handle.session_id).tracker.add(
        TokenUsage(input_tokens=10, output_tokens=10, total_tokens=20),
        "totally-made-up-model-xyz",
    )
    assert control.cost_root.subtree_has_estimated() is True
    assert control.cost_root.tracker.has_unknown_model_cost is False


@pytest.mark.asyncio
async def test_spawn_denied_when_fleet_token_budget_reached(tmp_path):
    # SpawnUsageGate reads the LIVE cumulative subtree spend off the cost tree:
    # once the fleet has burned its token budget, the next spawn is refused.
    control = make_control(tmp_path, max_total_tokens=1000)
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)
    # A first child is admitted (fleet spend still zero).
    child_role = _cost_role()
    handle = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: child_role,
            nickname="worker",
            parent_id=parent_rt.session_id,
        )
    )
    from mote.router.cost import TokenUsage

    # The child burns past the fleet token budget.
    control.cost_node_for(handle.session_id).tracker.add(
        TokenUsage(input_tokens=800, output_tokens=400, total_tokens=1200), "gpt-4o"
    )
    # The next spawn is refused — the tree is over its token budget.
    with pytest.raises(AgentLimitReached):
        await control.spawn_agent(
            SpawnSpec(
                role_factory=lambda ctx: _cost_role(),
                nickname="worker2",
                parent_id=parent_rt.session_id,
            )
        )


@pytest.mark.asyncio
async def test_spawn_denied_when_fleet_cost_budget_reached(tmp_path):
    control = make_control(tmp_path, max_cost_usd=0.001)
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)
    child_role = _cost_role()
    handle = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: child_role,
            nickname="worker",
            parent_id=parent_rt.session_id,
        )
    )
    from mote.router.cost import TokenUsage

    # A big known-model spend pushes fleet USD cost past the tiny cap.
    control.cost_node_for(handle.session_id).tracker.add(
        TokenUsage(input_tokens=100_000, output_tokens=100_000, total_tokens=200_000),
        "gpt-4o",
    )
    assert control.cost_root.subtree_cost() >= 0.001
    with pytest.raises(AgentLimitReached):
        await control.spawn_agent(
            SpawnSpec(
                role_factory=lambda ctx: _cost_role(),
                nickname="worker2",
                parent_id=parent_rt.session_id,
            )
        )


@pytest.mark.asyncio
async def test_usage_gate_inert_without_caps(tmp_path):
    # No token/cost caps -> the usage gate never denies regardless of spend.
    control = make_control(tmp_path)
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)
    child_role = _cost_role()
    handle = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: child_role,
            nickname="worker",
            parent_id=parent_rt.session_id,
        )
    )
    from mote.router.cost import TokenUsage

    control.cost_node_for(handle.session_id).tracker.add(
        TokenUsage(input_tokens=10**6, output_tokens=10**6, total_tokens=2 * 10**6),
        "gpt-4o",
    )
    # Still admits — no cap configured.
    handle2 = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: _cost_role(),
            nickname="worker2",
            parent_id=parent_rt.session_id,
        )
    )
    assert handle2 is not None


@pytest.mark.asyncio
async def test_usage_gate_denies_spawn_over_token_budget(tmp_path):
    # Once the fleet's live spend crosses the token ceiling, the next spawn is
    # refused — the runaway-fan-out guard's cost dimension.
    control = make_control(tmp_path, max_total_tokens=1000)
    parent_role = _cost_role()
    parent_rt = AgentRuntime(parent_role)
    control.add_agent(parent_rt, root=True)
    child_role = _cost_role()
    handle = await control.spawn_agent(
        SpawnSpec(
            role_factory=lambda ctx: child_role,
            nickname="worker",
            parent_id=parent_rt.session_id,
        )
    )
    from mote.router.cost import TokenUsage

    control.cost_node_for(handle.session_id).tracker.add(
        TokenUsage(input_tokens=800, output_tokens=400, total_tokens=1200), "gpt-4o"
    )
    with pytest.raises(AgentLimitReached):
        await control.spawn_agent(
            SpawnSpec(
                role_factory=lambda ctx: _cost_role(),
                nickname="worker2",
                parent_id=parent_rt.session_id,
            )
        )


@pytest.mark.asyncio
async def test_spawn_provisions_fresh_context_by_default(control):
    # A context-less role from the factory MUST come out of spawn_agent with a
    # real Context: provisioning is the authority's invariant, not the factory's.
    from mote.router.llm.context import Context

    child_role = SpawnRole()
    assert not hasattr(child_role, "_context")
    handle = await control.spawn_agent(SpawnSpec(role_factory=lambda ctx: child_role, nickname="worker"))
    assert isinstance(child_role._context, Context)
    # Independent tracker -> a distinct cost node whose bucket is the child's own.
    node = control.cost_node_for(handle.session_id)
    assert node is not None
    assert node.tracker is child_role._context.cost_manager


@pytest.mark.asyncio
async def test_fresh_children_get_independent_contexts(control):
    a = SpawnRole()
    b = SpawnRole()
    await control.spawn_agent(SpawnSpec(role_factory=lambda ctx: a, nickname="a"))
    await control.spawn_agent(SpawnSpec(role_factory=lambda ctx: b, nickname="b"))
    # Two FRESH spawns never share a context / tracker.
    assert a._context is not b._context
    assert a._context.cost_manager is not b._context.cost_manager


@pytest.mark.asyncio
async def test_share_parent_without_live_parent_raises(control):
    # SHARE_PARENT is a hard contract: no live parent context is a wiring bug,
    # surfaced loudly rather than silently patched with a fresh one.
    child_role = SpawnRole()
    with pytest.raises(RuntimeError):
        await control.spawn_agent(
            SpawnSpec(
                role_factory=lambda ctx: child_role,
                nickname="orphan",
                parent_id="nonexistent",
                context_policy=ContextPolicy.SHARE_PARENT,
            )
        )


@pytest.mark.asyncio
async def test_scheduler_binds_ambient_control_during_turn(control):
    seen = {}

    role = make_runtime("probe").role

    async def run(with_message=None):
        seen["control"] = current_control()
        return "ok"

    role.run = run
    rt = AgentRuntime(role)
    control.add_agent(rt)
    control.send_input(rt.session_id, UserMessage("hi"))
    await control.run(1)
    assert seen["control"] is control


# ---------------------------------------------------------------------------
# Workflow C: communication graph (channels + subtree broadcast)
# ---------------------------------------------------------------------------
from mote.environment.comms import CommKind  # noqa: E402


@pytest.mark.asyncio
async def test_spawn_registers_in_comm_graph(control):
    handle = await control.spawn_agent(_spec(nickname="worker"))
    assert control.comm_graph.path_for(handle.session_id) == handle.agent_path


@pytest.mark.asyncio
async def test_add_agent_root_registers_root_path_in_comm_graph(control):
    rt = make_runtime("root-1")
    control.add_agent(rt, root=True)
    assert control.comm_graph.path_for("root-1") == AgentPath.root()


def test_send_to_channel_fans_out(control):
    a, b, c = make_runtime("a"), make_runtime("b"), make_runtime("c")
    control.add_agent(a)
    control.add_agent(b)
    control.add_agent(c)
    control.comm_graph.join_channel("alerts", "a")
    control.comm_graph.join_channel("alerts", "b")
    delivered = control.send_to_channel("alerts", UserMessage("ping"))
    assert set(delivered) == {"a", "b"}
    assert not a.mailbox.empty()
    assert not b.mailbox.empty()
    assert c.mailbox.empty()


@pytest.mark.asyncio
async def test_broadcast_subtree_reaches_descendants_only(control):
    # root -> parent (MANAGED) -> two MANAGED grandchildren under the parent
    root_rt = make_runtime("root-1")
    control.add_agent(root_rt, root=True)
    parent = await control.spawn_agent(_spec(nickname="parent", lifecycle=Lifecycle.MANAGED, parent_id="root-1"))
    child1 = await control.spawn_agent(_spec(nickname="kid", lifecycle=Lifecycle.MANAGED, parent_id=parent.session_id))
    child2 = await control.spawn_agent(_spec(nickname="kid", lifecycle=Lifecycle.MANAGED, parent_id=parent.session_id))
    comm = InterAgentCommunication.new(
        author=parent.agent_path,
        recipient=parent.agent_path,
        content="status?",
        kind=CommKind.QUERY,
    )
    delivered = control.broadcast_subtree(parent.session_id, comm)
    # excludes the parent itself (include_root=False default), reaches both kids
    assert set(delivered) == {child1.session_id, child2.session_id}


def test_broadcast_subtree_unknown_root_returns_empty(control):
    comm = InterAgentCommunication.new(author=AgentPath.root(), recipient=AgentPath.root(), content="x")
    assert control.broadcast_subtree("ghost", comm) == []


@pytest.mark.asyncio
async def test_release_child_clears_comm_graph(control):
    handle = await control.spawn_agent(_spec(nickname="worker"))
    sid = handle.session_id
    control.comm_graph.join_channel("alerts", sid)
    control.release_child(sid)
    assert control.comm_graph.path_for(sid) is None
    assert control.comm_graph.channel_members("alerts") == []


def test_completion_notification_carries_notification_kind(control):
    from mote.environment.mailbox import MAILBOX_KIND

    rt = make_runtime("a")
    control.add_agent(rt)
    comm = InterAgentCommunication.new(
        author=AgentPath.root(),
        recipient=AgentPath.root(),
        content="done",
        kind=CommKind.NOTIFICATION,
    )
    msg = comm.to_message()
    assert msg.metadata.get(MAILBOX_KIND) == CommKind.NOTIFICATION.value
