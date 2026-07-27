from mote.contracts.events.types import OutputSnapshotEvent, OutputSnapshotInvalidatedEvent
from mote.kernel.output_stream import OutputSnapshotAccumulator, bind_output_snapshot_accumulator
from mote.runtime.events import bind_telemetry
from mote.runtime.events.stream import log_llm_stream
from mote.ztest.telemetry import InlineTelemetry


class Capture:
    def __init__(self):
        self.events = []

    def handle_sync(self, event):
        if isinstance(event, (OutputSnapshotEvent, OutputSnapshotInvalidatedEvent)):
            self.events.append(event)

    async def handle(self, event):
        return None


def test_native_schema_stream_emits_provisional_snapshot():
    capture = Capture()
    telemetry = InlineTelemetry(capture)
    accumulator = OutputSnapshotAccumulator(run_id="run-1", schema_fingerprint="sha")

    with bind_telemetry(telemetry), bind_output_snapshot_accumulator(accumulator):
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
    capture = Capture()
    telemetry = InlineTelemetry(capture)
    accumulator = OutputSnapshotAccumulator(run_id="run-1", schema_fingerprint="sha")

    with bind_telemetry(telemetry), bind_output_snapshot_accumulator(accumulator):
        log_llm_stream("7")
        log_llm_stream("x")

    assert [type(event) for event in capture.events] == [
        OutputSnapshotEvent,
        OutputSnapshotInvalidatedEvent,
    ]
    assert capture.events[1].revision == 1
    assert capture.events[1].reason == "stream_changed"


def test_normal_stream_does_not_emit_output_snapshots():
    capture = Capture()
    telemetry = InlineTelemetry(capture)

    with bind_telemetry(telemetry):
        log_llm_stream('{"count":7}')

    assert capture.events == []
