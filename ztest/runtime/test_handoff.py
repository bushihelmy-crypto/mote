from __future__ import annotations

import asyncio

import pytest

from mote.contracts.interaction.handoff import (
    DriverHandoffHandle,
    DriverHandoffResult,
    HandoffRequest,
    HandoffStatus,
    HumanHandoffOutcome,
)
from mote.contracts.runtime import (
    CheckpointFidelity,
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeHealth,
    RuntimeRef,
    RuntimeState,
)
from mote.contracts.surface import (
    TERMINAL_MEDIA_TYPE,
    SurfaceDescriptor,
    SurfaceFrame,
    SurfaceInput,
    SurfacePresentationMode,
)
from mote.runtime.interactive import HandoffCoordinator, RuntimeHost
from mote.runtime.interactive.terminal.driver import TerminalRuntimeDriver
from mote.runtime.session import SessionLog, SessionMetaEvent
from mote.runtime.session.replay import replay
from mote.runtime.session.runtime_handoff import SessionRuntimeHandoffJournal


class HandoffDriver:
    kind = "canvas"
    capabilities = RuntimeCapabilities(
        checkpoint_fidelity=CheckpointFidelity.FULL,
        handoff_modes=frozenset({"exclusive"}),
        surface_kinds=frozenset({"canvas"}),
    )

    def __init__(self) -> None:
        self.value = 0
        self.finished_with: HumanHandoffOutcome | None = None

    async def start(self, checkpoint=None):
        return DriverStartResult(restored=False)

    async def health(self):
        return RuntimeHealth(healthy=True)

    async def checkpoint(self, reason: str):
        return DriverCheckpoint(
            codec="canvas@1",
            schema_version=1,
            payload_ref=f"memory:{self.value}",
            digest=f"value:{self.value}",
        )

    async def prepare_handoff(self, request: HandoffRequest):
        return DriverHandoffHandle(
            handle_id="h-1",
            surface=SurfaceDescriptor(kind="canvas", ref="canvas:surface-1"),
        )

    async def finish_handoff(self, handle, outcome):
        self.finished_with = outcome
        return DriverHandoffResult(summary="canvas returned", resume_hint="inspect the drawing")

    async def snapshot_surface(self, handle):
        return SurfaceFrame(sequence=self.value, media_type="image/svg+xml", content="<svg/>")

    async def send_surface_input(self, handle, event):
        self.value += 1

    async def aclose(self):
        return None


class BlockingInteraction:
    def __init__(self, outcome: HumanHandoffOutcome) -> None:
        self.outcome = outcome
        self.opened = asyncio.Event()
        self.release = asyncio.Event()

    async def open_handoff(self, request, handle, surface=None):
        self.surface = surface
        self.opened.set()
        await self.release.wait()
        return self.outcome


class CheckpointSink:
    def __init__(self) -> None:
        self.items = []

    async def persist(self, checkpoint, *, reason: str) -> None:
        self.items.append((checkpoint, reason))


@pytest.mark.asyncio
async def test_handoff_fences_runtime_and_returns_human_message():
    sink = CheckpointSink()
    host = RuntimeHost(checkpoint_sink=sink)
    driver = HandoffDriver()
    descriptor = await host.create(driver, runtime_id="canvas-1")
    interaction = BlockingInteraction(
        HumanHandoffOutcome(status=HandoffStatus.COMPLETED, human_message="I moved the legend")
    )
    request = HandoffRequest(runtime_ref=descriptor.ref, message="Adjust the diagram")

    task = asyncio.create_task(HandoffCoordinator(host, interaction).handoff(request, owner_id="human:test"))
    await interaction.opened.wait()
    assert host.descriptor(descriptor.ref).state is RuntimeState.HANDED_OFF
    assert interaction.surface is not None
    assert (await interaction.surface.snapshot()).media_type == "image/svg+xml"
    await interaction.surface.send(SurfaceInput(kind="pointer", data='{"x":12,"y":8}'))

    interaction.release.set()
    outcome = await task

    assert outcome.status is HandoffStatus.COMPLETED
    assert outcome.human_message == "I moved the legend"
    assert outcome.summary == "canvas returned"
    assert outcome.resume_hint == "inspect the drawing"
    assert outcome.from_revision == 0
    assert outcome.to_revision == 1
    assert host.descriptor(descriptor.ref).state is RuntimeState.READY
    assert driver.finished_with == interaction.outcome
    assert [reason for _, reason in sink.items] == [
        "handoff-before",
        "handoff-after",
    ]


@pytest.mark.asyncio
async def test_cancelled_unchanged_handoff_does_not_advance_revision():
    host = RuntimeHost()
    descriptor = await host.create(HandoffDriver(), runtime_id="canvas-2")
    interaction = BlockingInteraction(HumanHandoffOutcome(status=HandoffStatus.CANCELLED, human_message="not now"))
    interaction.release.set()

    outcome = await HandoffCoordinator(host, interaction).handoff(
        HandoffRequest(runtime_ref=descriptor.ref),
        owner_id="human:test",
    )

    assert outcome.status is HandoffStatus.CANCELLED
    assert outcome.human_message == "not now"
    assert outcome.to_revision == 0


@pytest.mark.asyncio
async def test_completed_handoff_durably_resolves_with_final_revision(tmp_path):
    log = SessionLog("handoff-resolution", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent(session_id="handoff-resolution"))
    journal = SessionRuntimeHandoffJournal(log)
    host = RuntimeHost(handoff_journal=journal)
    descriptor = await host.create(HandoffDriver(), runtime_id="canvas-durable")
    interaction = BlockingInteraction(HumanHandoffOutcome(status=HandoffStatus.COMPLETED))
    interaction.release.set()

    outcome = await HandoffCoordinator(host, interaction).handoff(
        HandoffRequest(runtime_ref=descriptor.ref),
        owner_id="human:test",
    )

    state = replay(log)
    checkpoint = state.runtime_checkpoints["canvas:default"]
    assert outcome.to_revision == 1
    assert checkpoint.runtime_id == descriptor.ref.runtime_id
    assert checkpoint.revision == 1
    assert state.pending_runtime_handoffs == {}


@pytest.mark.asyncio
async def test_terminal_driver_live_surface_round_trip():
    driver = TerminalRuntimeDriver(session_key="handoff-surface-test", cwd=None)
    await driver.start()
    request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="terminal-1", kind="terminal"))
    handle = await driver.prepare_handoff(request)
    try:
        assert handle.surface.presentation is SurfacePresentationMode.WINDOW
        await driver.send_surface_input(
            handle,
            SurfaceInput(kind="terminal.resize", data='{"cols":100,"rows":30}'),
        )
        await driver.send_surface_input(
            handle,
            SurfaceInput(
                kind="terminal.input",
                data="stty size\n",
            ),
        )
        for _ in range(20):
            frame = await driver.snapshot_surface(handle)
            if "30 100" in frame.content:
                break
            await asyncio.sleep(0.05)
        assert frame.media_type == TERMINAL_MEDIA_TYPE
        assert "30 100" in frame.content
        assert "stty size" in frame.content
        await driver.capture_state()
        frame = await driver.snapshot_surface(handle)
        assert "__ENVPROBE_" not in frame.content
        sequence = frame.sequence
        await driver.finish_handoff(
            handle,
            HumanHandoffOutcome(status=HandoffStatus.COMPLETED, human_message="done"),
        )
        update = asyncio.create_task(driver.next_surface_frame(handle, sequence))
        await driver.feed("printf 'agent-after-handoff\\n'", 1_000)
        observed = await asyncio.wait_for(update, timeout=1)
        assert observed is not None
        assert "agent-after-handoff" in observed.content
        assert "printf 'agent-after-handoff" in observed.content
        with pytest.raises(RuntimeError, match="handoff handle"):
            await driver.send_surface_input(
                handle,
                SurfaceInput(kind="terminal.input", data="pwd\n"),
            )
        await driver.detach_surface(handle)
    finally:
        await driver.aclose()


@pytest.mark.asyncio
async def test_terminal_state_probe_does_not_enter_foreground_program():
    driver = TerminalRuntimeDriver(session_key="handoff-foreground-test", cwd=None)
    await driver.start()
    request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="terminal-foreground", kind="terminal"))
    handle = await driver.prepare_handoff(request)
    try:
        await driver.send_surface_input(
            handle,
            SurfaceInput(kind="terminal.input", data="sleep 30\n"),
        )
        assert await driver.capture_state() is None
        frame = await driver.snapshot_surface(handle)
        assert "__ENVPROBE_" not in frame.content
        await driver.interrupt(1_000)
    finally:
        await driver.aclose()
