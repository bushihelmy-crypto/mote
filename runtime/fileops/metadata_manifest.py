"""Canonical durable representation of preserved filesystem metadata."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

METADATA_MANIFEST_SCHEMA = "mote.fileops.preserved-metadata"
METADATA_MANIFEST_VERSION = 1
MAX_METADATA_MANIFEST_BYTES = 1_024 * 1_024
MAX_METADATA_XATTRS = 1_024
MAX_METADATA_XATTR_NAME_BYTES = 255
MAX_METADATA_XATTR_VALUE_BYTES = 64 * 1_024

_MAX_FILE_MODE = 0o7777
_MAX_OWNER_ID = (1 << 32) - 1
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "version",
        "mode",
        "uid",
        "gid",
        "xattrs_supported",
        "xattrs",
    }
)
_XATTR_KEYS = frozenset({"name", "value"})


class MetadataManifestError(ValueError):
    """The payload is not a bounded canonical metadata manifest."""


@dataclass(frozen=True)
class PreservedMetadata:
    mode: int
    uid: int | None
    gid: int | None
    xattrs: tuple[tuple[str, bytes], ...]
    xattrs_supported: bool

    @property
    def digest(self) -> str:
        return hashlib.sha256(encode_metadata_manifest(self)).hexdigest()

    @classmethod
    def for_create(cls, mode: int = 0o600) -> "PreservedMetadata":
        return cls(
            mode=mode,
            uid=None,
            gid=None,
            xattrs=(),
            xattrs_supported=hasattr(os, "listxattr"),
        )


def encode_metadata_manifest(metadata: PreservedMetadata) -> bytes:
    """Encode metadata into its only durable JSON representation."""

    _validate_metadata(metadata)
    payload = {
        "schema": METADATA_MANIFEST_SCHEMA,
        "version": METADATA_MANIFEST_VERSION,
        "mode": metadata.mode,
        "uid": metadata.uid,
        "gid": metadata.gid,
        "xattrs_supported": metadata.xattrs_supported,
        "xattrs": [
            {
                "name": name,
                "value": base64.b64encode(value).decode("ascii"),
            }
            for name, value in metadata.xattrs
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > MAX_METADATA_MANIFEST_BYTES:
        raise MetadataManifestError("metadata manifest exceeds the size limit")
    return encoded


def decode_metadata_manifest(payload: bytes) -> PreservedMetadata:
    """Decode a bounded manifest using exact schema and field types."""

    if type(payload) is not bytes:
        raise MetadataManifestError("metadata manifest must be bytes")
    if len(payload) > MAX_METADATA_MANIFEST_BYTES:
        raise MetadataManifestError("metadata manifest exceeds the size limit")
    try:
        native = json.loads(payload.decode("ascii", errors="strict"))
        if type(native) is not dict or set(native) != _MANIFEST_KEYS:
            raise ValueError("metadata manifest fields are not canonical")
        if type(native["schema"]) is not str:
            raise TypeError("metadata manifest schema must be a string")
        if native["schema"] != METADATA_MANIFEST_SCHEMA:
            raise ValueError("unsupported metadata manifest schema")
        if type(native["version"]) is not int:
            raise TypeError("metadata manifest version must be an integer")
        if native["version"] != METADATA_MANIFEST_VERSION:
            raise ValueError("unsupported metadata manifest version")
        xattrs = _decode_xattrs(native["xattrs"])
        metadata = PreservedMetadata(
            mode=native["mode"],
            uid=native["uid"],
            gid=native["gid"],
            xattrs=xattrs,
            xattrs_supported=native["xattrs_supported"],
        )
        _validate_metadata(metadata)
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        if isinstance(exc, MetadataManifestError):
            raise
        raise MetadataManifestError("metadata manifest is invalid") from exc
    return metadata


def _decode_xattrs(native: Any) -> tuple[tuple[str, bytes], ...]:
    if type(native) is not list:
        raise TypeError("metadata xattrs must be an array")
    if len(native) > MAX_METADATA_XATTRS:
        raise ValueError("metadata xattr count exceeds the limit")
    decoded: list[tuple[str, bytes]] = []
    for item in native:
        if type(item) is not dict or set(item) != _XATTR_KEYS:
            raise ValueError("metadata xattr fields are not canonical")
        name = item["name"]
        encoded_value = item["value"]
        if type(name) is not str or type(encoded_value) is not str:
            raise TypeError("metadata xattr name and value must be strings")
        encoded_value.encode("ascii", errors="strict")
        value = base64.b64decode(encoded_value, validate=True)
        if base64.b64encode(value).decode("ascii") != encoded_value:
            raise ValueError("metadata xattr value is not canonical base64")
        decoded.append((name, value))
    return tuple(decoded)


def _validate_metadata(metadata: PreservedMetadata) -> None:
    if type(metadata) is not PreservedMetadata:
        raise MetadataManifestError("metadata manifest source is invalid")
    if type(metadata.mode) is not int or not 0 <= metadata.mode <= _MAX_FILE_MODE:
        raise MetadataManifestError("metadata mode is invalid")
    _validate_owner_id(metadata.uid, "uid")
    _validate_owner_id(metadata.gid, "gid")
    if type(metadata.xattrs_supported) is not bool:
        raise MetadataManifestError("metadata xattrs_supported is invalid")
    if type(metadata.xattrs) is not tuple:
        raise MetadataManifestError("metadata xattrs are invalid")
    if len(metadata.xattrs) > MAX_METADATA_XATTRS:
        raise MetadataManifestError("metadata xattr count exceeds the limit")
    previous_name: str | None = None
    for item in metadata.xattrs:
        if type(item) is not tuple or len(item) != 2:
            raise MetadataManifestError("metadata xattr entry is invalid")
        name, value = item
        if type(name) is not str or not name or "\x00" in name:
            raise MetadataManifestError("metadata xattr name is invalid")
        try:
            encoded_name = name.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise MetadataManifestError("metadata xattr name is invalid") from exc
        if len(encoded_name) > MAX_METADATA_XATTR_NAME_BYTES:
            raise MetadataManifestError("metadata xattr name exceeds the size limit")
        if previous_name is not None and name <= previous_name:
            raise MetadataManifestError("metadata xattrs must have unique names in canonical order")
        if type(value) is not bytes:
            raise MetadataManifestError("metadata xattr value is invalid")
        if len(value) > MAX_METADATA_XATTR_VALUE_BYTES:
            raise MetadataManifestError("metadata xattr value exceeds the size limit")
        previous_name = name


def _validate_owner_id(value: int | None, field: str) -> None:
    if value is None:
        return
    if type(value) is not int or not 0 <= value <= _MAX_OWNER_ID:
        raise MetadataManifestError(f"metadata {field} is invalid")


__all__ = [
    "MAX_METADATA_MANIFEST_BYTES",
    "MAX_METADATA_XATTR_NAME_BYTES",
    "MAX_METADATA_XATTR_VALUE_BYTES",
    "MAX_METADATA_XATTRS",
    "METADATA_MANIFEST_SCHEMA",
    "METADATA_MANIFEST_VERSION",
    "MetadataManifestError",
    "PreservedMetadata",
    "decode_metadata_manifest",
    "encode_metadata_manifest",
]
