"""Small self-contained checkpoint codec used before artifact externalization."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from typing import Any

from mote.contracts.runtimes import CheckpointFidelity, DriverCheckpoint, RuntimeCheckpoint

_DATA_PREFIX = "data:application/json;base64,"


def encode_inline_json(
    payload: Any,
    *,
    codec: str,
    fidelity: CheckpointFidelity,
    sensitivity: str = "private",
) -> DriverCheckpoint:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    return DriverCheckpoint(
        codec=codec,
        schema_version=1,
        payload_ref=f"{_DATA_PREFIX}{encoded}",
        digest=hashlib.sha256(raw).hexdigest(),
        sensitivity=sensitivity,
        fidelity=fidelity,
    )


def decode_inline_json(checkpoint: RuntimeCheckpoint, *, codec: str) -> Any:
    if checkpoint.codec != codec:
        raise ValueError(f"unsupported checkpoint codec: {checkpoint.codec}")
    if not checkpoint.payload_ref.startswith(_DATA_PREFIX):
        raise ValueError("checkpoint payload is not an inline JSON data reference")
    raw = decode_inline_bytes(checkpoint)
    return json.loads(raw)


def decode_inline_bytes(checkpoint: RuntimeCheckpoint) -> bytes:
    """Decode and authenticate one inline checkpoint payload without parsing it."""
    if not checkpoint.payload_ref.startswith(_DATA_PREFIX):
        raise ValueError("checkpoint payload is not an inline JSON data reference")
    raw = base64.urlsafe_b64decode(checkpoint.payload_ref[len(_DATA_PREFIX) :].encode("ascii"))
    if checkpoint.digest and hashlib.sha256(raw).hexdigest() != checkpoint.digest:
        raise ValueError("checkpoint digest mismatch")
    return raw


def inline_checkpoint(checkpoint: RuntimeCheckpoint, raw: bytes) -> RuntimeCheckpoint:
    """Return a checkpoint carrying verified JSON bytes inline for a driver."""
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
    "decode_inline_bytes",
    "decode_inline_json",
    "encode_inline_json",
    "inline_checkpoint",
]
