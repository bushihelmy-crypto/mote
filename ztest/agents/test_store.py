from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pytest

from mote.contracts.agent import ContextPolicy, SpawnContext
from mote.contracts.content import ContentDigest
from mote.contracts.conversation import Message, MessageQueue, UserMessage
from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.runtime.errors import LeaseFencedError
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.messaging.mailbox import Mailbox
from mote.orchestration.agents.residency.model import ResidencyIdentity
from mote.orchestration.agents.residency.store import ResidencyStore, ResidencyStoreError
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.session import SessionLog
from mote.runtime.session.events import MessageEvent, SessionMetaEvent

DIGEST = ContentDigest("a" * 64)


class FakeResidentAgent:
    def __init__(self, session_id: str, *, payload: str = "state") -> None:
        self._session_id = session_id
        self.payload = payload
        self.messages: list[Message] = []
        self.state = type("State", (), {})()
        self.state.msg_buffer = MessageQueue()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def residency_definition_id(self) -> str:
        return "fake.agent.v1"

    @property
    def residency_config_digest(self) -> ContentDigest:
        return DIGEST

    def export_residency_state(self, *, session_history_is_durable: bool) -> Mapping[str, JsonValue]:
        if not session_history_is_durable:
            raise ValueError("fake requires durable Session history")
        frozen = freeze_json({"payload": self.payload, "messages": []})
        assert isinstance(frozen, Mapping)
        return frozen

    def restore_residency_message_buffer(self, snapshot: JsonValue) -> None:
        self.state.msg_buffer = MessageQueue.load(json.dumps(thaw_json(snapshot)))

    def restore_residency_history(self, messages: tuple[Message, ...], session_meta: Mapping[str, object]) -> None:
        if session_meta["session_id"] != self.session_id:
            raise ValueError("Session identity mismatch")
        self.messages[:] = messages

    async def run(self, with_message: Message | None = None):
        return None

    async def cleanup(self) -> None:
        return None

    def build_child_spawn_context(self, *, parent_id: str | None, agent_path: str) -> SpawnContext:
        return SpawnContext(parent_id=parent_id, agent_path=agent_path)

    def provision_spawned_child(self, child, policy: ContextPolicy) -> None:
        return None

    def provision_unparented_spawn(self, spawn_context: SpawnContext) -> None:
        return None

    def spawn_cost_attribution(self):
        raise NotImplementedError

    def bind_agent_control(self, control) -> None:
        return None


class FakeFactory:
    definition_id = "fake.agent.v1"
    config_digest = DIGEST

    def build(self, state: Mapping[str, JsonValue]) -> FakeResidentAgent:
        if set(state) != {"payload", "messages"}:
            raise ValueError("fake state fields are not canonical")
        payload = state["payload"]
        if type(payload) is not str:
            raise ValueError("fake payload is invalid")
        return FakeResidentAgent("agent-1", payload=payload)


def _identity(*, root_agent_id: str = "root-1", incarnation_generation: int = 1) -> ResidencyIdentity:
    return ResidencyIdentity(
        logical_agent_id="agent-1",
        root_agent_id=root_agent_id,
        parent_agent_id="root-1",
        agent_path="/root/worker",
        nickname="worker",
        definition_id="fake.agent.v1",
        config_digest=DIGEST,
        incarnation_generation=incarnation_generation,
    )


def _seed_session(path: Path, *, messages: tuple[str, ...] = ("durable",)) -> None:
    log = SessionLog("agent-1", base_dir=str(path))
    log.commit_offline(SessionMetaEvent("agent-1", "fake.agent.v1", ()))
    for message in messages:
        log.commit_offline(MessageEvent(message=UserMessage(message)))


def _components(tmp_path: Path):
    now = [1.0]
    coordinator = InMemoryLeaseCoordinator(clock=lambda: now[0])
    store = ResidencyStore(
        str(tmp_path / "residency"),
        sessions_base_dir=str(tmp_path / "sessions"),
        lease_coordinator=coordinator,
    )
    lease = coordinator.acquire("agent-residency:agent-1", "owner-1", 30)
    return now, coordinator, store, lease


@pytest.mark.asyncio
async def test_materialize_and_trusted_factory_rehydrate_round_trip(tmp_path: Path) -> None:
    _seed_session(tmp_path / "sessions")
    _, _, store, lease = _components(tmp_path)
    agent = FakeResidentAgent("agent-1", payload="kept")
    agent.state.msg_buffer.push(UserMessage("buffered"))
    mailbox = Mailbox("agent-1")
    mailbox.enqueue(UserMessage("mail"))
    record = await store.materialize(AgentRuntime(agent, mailbox), identity=_identity(), lease=lease)
    assert record.source_session_revision == 2
    assert record.record_revision == 1
    restored = store.rehydrate(_identity(), factory=FakeFactory(), lease=lease)
    assert restored is not None
    restored_agent, restored_mailbox = restored.agent, restored.mailbox
    assert isinstance(restored_agent, FakeResidentAgent)
    assert restored_agent.payload == "kept"
    assert [message.content for message in restored_agent.messages] == ["durable"]
    assert not restored_agent.state.msg_buffer.empty()
    assert not restored_mailbox.empty()


def test_missing_record_is_not_confused_with_corruption(tmp_path: Path) -> None:
    _, _, store, lease = _components(tmp_path)
    assert store.read_record("agent-1") is None
    assert store.rehydrate(_identity(), factory=FakeFactory(), lease=lease) is None


@pytest.mark.asyncio
async def test_materialize_requires_committed_session_truth(tmp_path: Path) -> None:
    _, _, store, lease = _components(tmp_path)
    with pytest.raises(ResidencyStoreError, match="Session stream"):
        await store.materialize(AgentRuntime(FakeResidentAgent("agent-1")), identity=_identity(), lease=lease)


@pytest.mark.asyncio
async def test_record_revision_advances_and_forget_is_revision_fenced(tmp_path: Path) -> None:
    _seed_session(tmp_path / "sessions")
    _, _, store, lease = _components(tmp_path)
    runtime = AgentRuntime(FakeResidentAgent("agent-1"))
    first = await store.materialize(runtime, identity=_identity(), lease=lease)
    second = await store.materialize(runtime, identity=_identity(), lease=lease)
    assert (first.record_revision, second.record_revision) == (1, 2)
    claimed = store.rehydrate(_identity(), factory=FakeFactory(), lease=lease)
    assert claimed is not None
    with pytest.raises(ResidencyStoreError, match="revision conflict"):
        store.forget("agent-1", expected_record_revision=1, lease=lease)
    assert store.forget("agent-1", expected_record_revision=claimed.install_record_revision, lease=lease)
    assert not store.has("agent-1")


@pytest.mark.asyncio
async def test_identity_definition_and_config_mismatch_fail_closed(tmp_path: Path) -> None:
    _seed_session(tmp_path / "sessions")
    _, _, store, lease = _components(tmp_path)
    await store.materialize(AgentRuntime(FakeResidentAgent("agent-1")), identity=_identity(), lease=lease)
    for identity in (
        _identity(root_agent_id="other-root"),
        _identity(incarnation_generation=2),
    ):
        with pytest.raises(ResidencyStoreError, match="trusted composition"):
            store.rehydrate(identity, factory=FakeFactory(), lease=lease)

    wrong_factory = FakeFactory()
    wrong_factory.definition_id = "attacker.agent"
    with pytest.raises(ResidencyStoreError, match="definition identity"):
        store.rehydrate(_identity(), factory=wrong_factory, lease=lease)


@pytest.mark.asyncio
async def test_misplaced_record_and_restored_session_mismatch_preserve_evidence(
    tmp_path: Path,
) -> None:
    _seed_session(tmp_path / "sessions")
    _, _, store, lease = _components(tmp_path)
    await store.materialize(AgentRuntime(FakeResidentAgent("agent-1")), identity=_identity(), lease=lease)
    path = tmp_path / "residency" / "agent-1.json"
    evidence = path.read_bytes()

    misplaced = tmp_path / "residency" / "agent-2.json"
    misplaced.write_bytes(evidence)
    with pytest.raises(ResidencyStoreError, match="invalid"):
        store.read_record("agent-2")
    assert misplaced.read_bytes() == evidence

    class WrongSessionFactory(FakeFactory):
        def build(self, state: Mapping[str, JsonValue]) -> FakeResidentAgent:
            restored = super().build(state)
            restored._session_id = "agent-2"
            return restored

    with pytest.raises(ResidencyStoreError, match="logical identity"):
        store.rehydrate(_identity(), factory=WrongSessionFactory(), lease=lease)
    assert path.read_bytes() == evidence


@pytest.mark.asyncio
async def test_stale_install_claim_cannot_delete_successor_claim(tmp_path: Path) -> None:
    _seed_session(tmp_path / "sessions")
    now, coordinator, store, first_lease = _components(tmp_path)
    await store.materialize(AgentRuntime(FakeResidentAgent("agent-1")), identity=_identity(), lease=first_lease)
    first = store.rehydrate(_identity(), factory=FakeFactory(), lease=first_lease)
    assert first is not None

    now[0] = 40.0
    second_lease = coordinator.acquire("agent-residency:agent-1", "owner-2", 30)
    second = store.rehydrate(_identity(), factory=FakeFactory(), lease=second_lease)
    assert second is not None
    with pytest.raises(LeaseFencedError):
        store.forget(
            "agent-1",
            expected_record_revision=first.install_record_revision,
            lease=first_lease,
        )
    assert store.forget(
        "agent-1",
        expected_record_revision=second.install_record_revision,
        lease=second_lease,
    )


@pytest.mark.asyncio
async def test_corruption_and_disk_selected_class_do_not_activate(tmp_path: Path) -> None:
    _seed_session(tmp_path / "sessions")
    _, _, store, lease = _components(tmp_path)
    await store.materialize(AgentRuntime(FakeResidentAgent("agent-1")), identity=_identity(), lease=lease)
    path = tmp_path / "residency" / "agent-1.json"
    original = path.read_bytes()
    path.write_bytes(b"{torn")
    with pytest.raises(ResidencyStoreError, match="invalid"):
        store.rehydrate(_identity(), factory=FakeFactory(), lease=lease)
    path.write_bytes(original)
    raw = json.loads(original)
    raw["state_snapshot"]["backend_class"] = "attacker.Agent"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="state fields"):
        store.rehydrate(_identity(), factory=FakeFactory(), lease=lease)


@pytest.mark.asyncio
async def test_stale_fence_cannot_materialize_rehydrate_or_forget(tmp_path: Path) -> None:
    _seed_session(tmp_path / "sessions")
    now, coordinator, store, stale = _components(tmp_path)
    await store.materialize(AgentRuntime(FakeResidentAgent("agent-1")), identity=_identity(), lease=stale)
    record = store.read_record("agent-1")
    assert record is not None
    now[0] = 40.0
    coordinator.acquire("agent-residency:agent-1", "owner-2", 30)
    with pytest.raises(LeaseFencedError):
        store.rehydrate(_identity(), factory=FakeFactory(), lease=stale)
    with pytest.raises(LeaseFencedError):
        store.forget("agent-1", expected_record_revision=record.record_revision, lease=stale)
