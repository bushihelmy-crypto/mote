from mote.common.events import EventBus, set_bus
from mote.common.events.output_stream import OutputSnapshotAccumulator, bind_output_snapshot_accumulator
from mote.common.events.stream import log_llm_stream
from mote.common.events.types import OutputSnapshotEvent, OutputSnapshotInvalidatedEvent
from mote.common.interface.event_subscriber import ObservationSubscriber, SyncObserver


class Capture(ObservationSubscriber, SyncObserver):
    def __init__(self):
        self.events = []

    def handle_sync(self, event):
        if isinstance(event, (OutputSnapshotEvent, OutputSnapshotInvalidatedEvent)):
            self.events.append(event)

    async def handle(self, event):
        return None


def test_native_schema_stream_emits_provisional_snapshot():
    bus = EventBus()
    capture = Capture()
    bus.subscribe(capture)
    accumulator = OutputSnapshotAccumulator(run_id="run-1", schema_fingerprint="sha")

    with set_bus(bus), bind_output_snapshot_accumulator(accumulator):
        log_llm_stream('{"count":')
        log_llm_stream("7}")

    assert len(capture.events) == 1
    snapshot = capture.events[0]
    assert isinstance(snapshot, OutputSnapshotEvent)
    assert snapshot.run_id == "run-1"
    assert snapshot.revision == 1
    assert snapshot.schema_fingerprint == "sha"
    assert snapshot.value == {"count": 7}


def test_later_stream_data_invalidates_previous_snapshot():
    bus = EventBus()
    capture = Capture()
    bus.subscribe(capture)
    accumulator = OutputSnapshotAccumulator(run_id="run-1", schema_fingerprint="sha")

    with set_bus(bus), bind_output_snapshot_accumulator(accumulator):
        log_llm_stream("7")
        log_llm_stream("x")

    assert [type(event) for event in capture.events] == [
        OutputSnapshotEvent,
        OutputSnapshotInvalidatedEvent,
    ]
    assert capture.events[1].revision == 1
    assert capture.events[1].reason == "stream_changed"


def test_normal_stream_does_not_emit_output_snapshots():
    bus = EventBus()
    capture = Capture()
    bus.subscribe(capture)

    with set_bus(bus):
        log_llm_stream('{"count":7}')

    assert capture.events == []
