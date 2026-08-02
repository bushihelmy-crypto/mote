from __future__ import annotations

from dataclasses import replace

import pytest

from mote.contracts.runtime import CheckpointFidelity, DriverCheckpoint, RuntimeCheckpoint
from mote.contracts.surface import CanvasDocument, NotebookDocument
from mote.runtime.interactive.checkpoint_codec import (
    BROWSER_CHECKPOINT_CODEC,
    CANVAS_CHECKPOINT_CODEC,
    KERNEL_CHECKPOINT_CODEC,
    TERMINAL_CHECKPOINT_CODEC,
    BrowserCheckpointState,
    KernelCheckpointState,
    ShellCheckpointState,
    encode_inline_json,
)


def _checkpoint(kind: str, driver) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        runtime_id=f"{kind}-1",
        kind=kind,
        epoch=1,
        revision=1,
        codec=driver.codec,
        schema_version=driver.schema_version,
        payload_ref=driver.payload_ref,
        digest=driver.digest,
        sensitivity=driver.sensitivity,
        fidelity=driver.fidelity or CheckpointFidelity.LOGICAL,
    )


def test_all_current_runtime_codec_states_round_trip_typed() -> None:
    shell = ShellCheckpointState("/work", {"A": "1"}, ("OLD",))
    browser = BrowserCheckpointState(("https://example.test",), 0, {"cookies": (), "origins": ()})
    kernel = KernelCheckpointState(shell, NotebookDocument(ref="jupyter:default"))
    cases = (
        (TERMINAL_CHECKPOINT_CODEC, shell),
        (BROWSER_CHECKPOINT_CODEC, browser),
        (CANVAS_CHECKPOINT_CODEC, CanvasDocument()),
        (KERNEL_CHECKPOINT_CODEC, kernel),
    )
    for codec, state in cases:
        encoded = codec.encode(state, fidelity=CheckpointFidelity.LOGICAL)
        checkpoint = _checkpoint(codec.kind, encoded)
        assert codec.decode(checkpoint) == state


def test_historical_checkpoint_versions_fail_closed() -> None:
    browser_v1 = encode_inline_json(
        {"urls": ["https://example.test"], "active": 0},
        codec="browser-state+json@1",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    with pytest.raises(ValueError, match="unsupported"):
        BROWSER_CHECKPOINT_CODEC.decode(_checkpoint("browser", browser_v1))

    kernel_v1 = encode_inline_json(
        {"cwd": "/work", "env": {"A": "1"}, "unset": []},
        codec="jupyter-state+json@1",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    with pytest.raises(ValueError, match="unsupported"):
        KERNEL_CHECKPOINT_CODEC.decode(_checkpoint("jupyter", kernel_v1))

    kernel_v2_legacy_envelope = encode_inline_json(
        {
            "cwd": "/work",
            "env": {"A": "1"},
            "unset": [],
            "notebook": NotebookDocument(ref="jupyter:default").model_dump(mode="json"),
        },
        codec="jupyter-state+json@2",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    with pytest.raises(ValueError, match="unsupported"):
        KERNEL_CHECKPOINT_CODEC.decode(_checkpoint("jupyter", kernel_v2_legacy_envelope))


@pytest.mark.parametrize(
    ("kind", "codec", "payload"),
    (
        ("terminal", TERMINAL_CHECKPOINT_CODEC, {"cwd": "/w", "env": {}, "unset": [], "extra": 1}),
        ("terminal", TERMINAL_CHECKPOINT_CODEC, {"cwd": "/w", "env": {"A": 1}, "unset": []}),
        ("browser", BROWSER_CHECKPOINT_CODEC, {"urls": [], "active": "0", "storage_state": None}),
        ("browser", BROWSER_CHECKPOINT_CODEC, {"urls": [], "active": 0, "storage_state": {"cookies": {}}}),
        (
            "browser",
            BROWSER_CHECKPOINT_CODEC,
            {
                "urls": [],
                "active": 0,
                "storage_state": {
                    "cookies": [{"name": "sid"}],
                    "origins": [],
                },
            },
        ),
        (
            "browser",
            BROWSER_CHECKPOINT_CODEC,
            {
                "urls": [],
                "active": 0,
                "storage_state": {
                    "cookies": [],
                    "origins": [
                        {
                            "origin": "https://example.test",
                            "localStorage": [{"name": "token", "value": 1}],
                        }
                    ],
                },
            },
        ),
    ),
)
def test_registered_codecs_reject_wrong_or_non_exact_state(kind, codec, payload) -> None:
    encoded = encode_inline_json(
        payload,
        codec=codec.codec,
        fidelity=CheckpointFidelity.LOGICAL,
    )
    checkpoint = replace(_checkpoint(kind, encoded), schema_version=codec.schema_version)
    with pytest.raises((TypeError, ValueError)):
        codec.decode(checkpoint)


def test_unknown_schema_version_and_kind_fail_closed() -> None:
    encoded = TERMINAL_CHECKPOINT_CODEC.encode(ShellCheckpointState("/w", {}, ()), fidelity=CheckpointFidelity.LOGICAL)
    checkpoint = _checkpoint("terminal", encoded)
    with pytest.raises(ValueError, match="unsupported"):
        TERMINAL_CHECKPOINT_CODEC.decode(replace(checkpoint, schema_version=99))
    with pytest.raises(ValueError, match="kind does not match"):
        TERMINAL_CHECKPOINT_CODEC.decode(replace(checkpoint, kind="unknown"))


@pytest.mark.parametrize(
    "changes",
    (
        {"epoch": 0},
        {"epoch": "1"},
        {"revision": "1"},
        {"schema_version": "1"},
        {"sensitivity": "public"},
        {"alias": "bad:alias"},
    ),
)
def test_runtime_checkpoint_envelope_rejects_invalid_identity_and_primitives(changes) -> None:
    encoded = TERMINAL_CHECKPOINT_CODEC.encode(ShellCheckpointState("/w", {}, ()), fidelity=CheckpointFidelity.LOGICAL)
    checkpoint = _checkpoint("terminal", encoded)
    with pytest.raises((TypeError, ValueError)):
        replace(checkpoint, **changes)


@pytest.mark.parametrize(
    "changes",
    (
        {"codec": ""},
        {"payload_ref": ""},
        {"schema_version": True},
        {"sensitivity": "public"},
        {"fidelity": "logical"},
    ),
)
def test_driver_checkpoint_constructor_rejects_invalid_primitives(changes) -> None:
    values = {
        "codec": "driver+json@1",
        "schema_version": 1,
        "payload_ref": "data:application/json;base64,e30=",
        "fidelity": CheckpointFidelity.LOGICAL,
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        DriverCheckpoint(**values)
