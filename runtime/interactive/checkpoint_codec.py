"""Strict typed codecs for managed Runtime checkpoints."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Generic, TypeVar, cast

from mote.contracts.browser import decode_browser_storage_state
from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.runtime import CheckpointFidelity, DriverCheckpoint, RuntimeCheckpoint
from mote.contracts.surface.canvas import CanvasDocument
from mote.contracts.surface.notebook import NotebookDocument

_DATA_PREFIX = "data:application/json;base64,"
StateT = TypeVar("StateT")


@dataclass(frozen=True, slots=True)
class ShellCheckpointState:
    cwd: str
    env: Mapping[str, str]
    unset: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserCheckpointState:
    urls: tuple[str, ...]
    active: int
    storage_state: Mapping[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class KernelCheckpointState:
    shell: ShellCheckpointState
    notebook: NotebookDocument | None


Decoder = Callable[[object], StateT]
Encoder = Callable[[StateT], JsonValue]


@dataclass(frozen=True, slots=True)
class CheckpointCodec(Generic[StateT]):
    """One strict current codec owned by a managed Runtime driver."""

    kind: str
    codec: str
    schema_version: int
    decode_payload: Decoder[StateT]
    encode_state: Encoder[StateT]

    def decode(self, checkpoint: RuntimeCheckpoint) -> StateT:
        if checkpoint.kind != self.kind:
            raise ValueError("checkpoint kind does not match codec owner")
        if type(checkpoint.schema_version) is not int or (
            checkpoint.codec,
            checkpoint.schema_version,
        ) != (
            self.codec,
            self.schema_version,
        ):
            raise ValueError(
                f"unsupported {self.kind} checkpoint codec/version: " f"{checkpoint.codec}/{checkpoint.schema_version}"
            )
        return self.decode_payload(_decode_json_payload(checkpoint))

    def encode(
        self,
        state: StateT,
        *,
        fidelity: CheckpointFidelity,
        sensitivity: str = "private",
    ) -> DriverCheckpoint:
        return _encode_json_payload(
            self.encode_state(state),
            codec=self.codec,
            schema_version=self.schema_version,
            fidelity=fidelity,
            sensitivity=sensitivity,
        )


def _exact_object(value: object, fields: set[str], *, owner: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{owner} fields must be exactly {sorted(fields)!r}")
    return value


def _string(value: object, *, owner: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{owner} must be a string")
    return value


def _string_mapping(value: object, *, owner: str) -> Mapping[str, str]:
    if type(value) is not dict or any(type(key) is not str or type(item) is not str for key, item in value.items()):
        raise TypeError(f"{owner} must be a string mapping")
    return dict(value)


def _string_tuple(value: object, *, owner: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise TypeError(f"{owner} must be a string list")
    return tuple(value)


def _decode_shell(value: object) -> ShellCheckpointState:
    payload = _exact_object(value, {"cwd", "env", "unset"}, owner="shell checkpoint")
    return ShellCheckpointState(
        cwd=_string(payload["cwd"], owner="shell checkpoint cwd"),
        env=_string_mapping(payload["env"], owner="shell checkpoint env"),
        unset=_string_tuple(payload["unset"], owner="shell checkpoint unset"),
    )


def _encode_shell(state: ShellCheckpointState) -> JsonValue:
    return cast(
        JsonValue,
        freeze_json({"cwd": state.cwd, "env": dict(state.env), "unset": list(state.unset)}),
    )


def _decode_browser(value: object) -> BrowserCheckpointState:
    payload = _exact_object(value, {"urls", "active", "storage_state"}, owner="browser checkpoint")
    urls = _string_tuple(payload["urls"], owner="browser checkpoint urls")
    active = payload["active"]
    if type(active) is not int or active < 0 or (urls and active >= len(urls)):
        raise ValueError("browser checkpoint active index is invalid")
    storage = payload["storage_state"]
    typed_storage: Mapping[str, JsonValue] | None
    if storage is None:
        typed_storage = None
    else:
        canonical = decode_browser_storage_state(storage)
        frozen = freeze_json(canonical.to_payload(), path="browser_checkpoint.storage_state")
        assert isinstance(frozen, Mapping)
        typed_storage = frozen
    return BrowserCheckpointState(urls, active, typed_storage)


def _encode_browser(state: BrowserCheckpointState) -> JsonValue:
    return cast(
        JsonValue,
        freeze_json(
            {
                "urls": list(state.urls),
                "active": state.active,
                "storage_state": state.storage_state,
            }
        ),
    )


def _decode_canvas(value: object) -> CanvasDocument:
    return CanvasDocument.model_validate(value, strict=True)


def _encode_canvas(state: CanvasDocument) -> JsonValue:
    return cast(JsonValue, freeze_json(state.model_dump(mode="json")))


def _decode_kernel_v2(value: object) -> KernelCheckpointState:
    payload = _exact_object(value, {"cwd", "env", "unset", "notebook"}, owner="kernel checkpoint")
    shell = _decode_shell({key: payload[key] for key in ("cwd", "env", "unset")})
    notebook = NotebookDocument.model_validate(payload["notebook"], strict=True)
    return KernelCheckpointState(shell, notebook)


def _encode_kernel(state: KernelCheckpointState) -> JsonValue:
    if state.notebook is None:
        raise ValueError("current kernel checkpoint requires notebook state")
    shell = _encode_shell(state.shell)
    assert isinstance(shell, Mapping)
    return cast(
        JsonValue,
        freeze_json({**shell, "notebook": state.notebook.model_dump(mode="json")}),
    )


TERMINAL_CHECKPOINT_CODEC = CheckpointCodec("terminal", "terminal-state+json@1", 1, _decode_shell, _encode_shell)
BROWSER_CHECKPOINT_CODEC = CheckpointCodec(
    "browser",
    "browser-state+json@2",
    2,
    _decode_browser,
    _encode_browser,
)
CANVAS_CHECKPOINT_CODEC = CheckpointCodec("canvas", "canvas-document+json@1", 1, _decode_canvas, _encode_canvas)
KERNEL_CHECKPOINT_CODEC = CheckpointCodec(
    "jupyter",
    "jupyter-state+json@2",
    2,
    _decode_kernel_v2,
    _encode_kernel,
)


def _encode_json_payload(
    payload: JsonValue,
    *,
    codec: str,
    schema_version: int,
    fidelity: CheckpointFidelity,
    sensitivity: str,
) -> DriverCheckpoint:
    raw = json.dumps(
        thaw_json(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    return DriverCheckpoint(
        codec=codec,
        schema_version=schema_version,
        payload_ref=f"{_DATA_PREFIX}{encoded}",
        digest=hashlib.sha256(raw).hexdigest(),
        sensitivity=sensitivity,
        fidelity=fidelity,
    )


def encode_inline_json(
    payload: JsonValue,
    *,
    codec: str,
    fidelity: CheckpointFidelity,
    sensitivity: str = "private",
) -> DriverCheckpoint:
    """Encode unregistered test/extension state; restore must still name its schema."""
    return _encode_json_payload(
        payload,
        codec=codec,
        schema_version=1,
        fidelity=fidelity,
        sensitivity=sensitivity,
    )


def decode_inline_json(checkpoint: RuntimeCheckpoint, *, codec: str) -> JsonValue:
    """Decode an explicitly version-1 unregistered test/extension checkpoint."""
    if checkpoint.codec != codec or type(checkpoint.schema_version) is not int or checkpoint.schema_version != 1:
        raise ValueError(f"unsupported checkpoint codec/version: {checkpoint.codec}/{checkpoint.schema_version}")
    return cast(JsonValue, _decode_json_payload(checkpoint))


def _decode_json_payload(checkpoint: RuntimeCheckpoint) -> object:
    raw = decode_inline_bytes(checkpoint)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("checkpoint payload is not canonical JSON") from exc
    return thaw_json(freeze_json(decoded, path="runtime_checkpoint.payload"))


def decode_inline_bytes(checkpoint: RuntimeCheckpoint) -> bytes:
    if not checkpoint.payload_ref.startswith(_DATA_PREFIX):
        raise ValueError("checkpoint payload is not an inline JSON data reference")
    try:
        raw = base64.b64decode(
            checkpoint.payload_ref[len(_DATA_PREFIX) :].encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("checkpoint payload reference is invalid base64") from exc
    if not checkpoint.digest or hashlib.sha256(raw).hexdigest() != checkpoint.digest:
        raise ValueError("checkpoint digest mismatch")
    return raw


def inline_checkpoint(checkpoint: RuntimeCheckpoint, raw: bytes) -> RuntimeCheckpoint:
    if type(raw) is not bytes:
        raise TypeError("checkpoint payload must be bytes")
    digest = hashlib.sha256(raw).hexdigest()
    if checkpoint.digest and checkpoint.digest != digest:
        raise ValueError("checkpoint digest mismatch")
    return replace(
        checkpoint,
        payload_ref=(_DATA_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")),
        digest=digest,
    )


__all__ = [
    "BROWSER_CHECKPOINT_CODEC",
    "BrowserCheckpointState",
    "CANVAS_CHECKPOINT_CODEC",
    "CheckpointCodec",
    "KERNEL_CHECKPOINT_CODEC",
    "KernelCheckpointState",
    "ShellCheckpointState",
    "TERMINAL_CHECKPOINT_CODEC",
    "decode_inline_bytes",
    "decode_inline_json",
    "encode_inline_json",
    "inline_checkpoint",
]
