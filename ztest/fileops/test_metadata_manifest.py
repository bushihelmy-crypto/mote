from __future__ import annotations

import base64
import json

import pytest

from mote.runtime.fileops.metadata import PreservedMetadata
from mote.runtime.fileops.metadata_manifest import (
    MAX_METADATA_MANIFEST_BYTES,
    MAX_METADATA_XATTR_NAME_BYTES,
    MAX_METADATA_XATTR_VALUE_BYTES,
    MAX_METADATA_XATTRS,
    METADATA_MANIFEST_SCHEMA,
    METADATA_MANIFEST_VERSION,
    MetadataManifestError,
    decode_metadata_manifest,
    encode_metadata_manifest,
)


def _metadata() -> PreservedMetadata:
    return PreservedMetadata(
        mode=0o640,
        uid=1000,
        gid=1001,
        xattrs=(("security.selinux", b"label"), ("user.binary", b"\x00\xff")),
        xattrs_supported=True,
    )


def _payload() -> dict[str, object]:
    return json.loads(encode_metadata_manifest(_metadata()).decode("ascii"))


def _encode(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def test_metadata_manifest_round_trip_is_deterministic():
    metadata = _metadata()
    first = encode_metadata_manifest(metadata)
    second = encode_metadata_manifest(metadata)

    assert first == second
    assert decode_metadata_manifest(first) == metadata
    assert _payload()["schema"] == METADATA_MANIFEST_SCHEMA
    assert _payload()["version"] == METADATA_MANIFEST_VERSION


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "other"),
        ("version", 2),
        ("version", True),
        ("mode", True),
        ("mode", "416"),
        ("uid", True),
        ("uid", "1000"),
        ("gid", True),
        ("gid", "1001"),
        ("xattrs_supported", 1),
        ("unexpected", "field"),
    ],
)
def test_metadata_manifest_rejects_wrong_versions_types_and_fields(field, value):
    payload = _payload()
    payload[field] = value

    with pytest.raises(MetadataManifestError):
        decode_metadata_manifest(_encode(payload))


@pytest.mark.parametrize("field", ["schema", "version", "mode", "uid", "gid"])
def test_metadata_manifest_rejects_missing_fields(field):
    payload = _payload()
    del payload[field]

    with pytest.raises(MetadataManifestError):
        decode_metadata_manifest(_encode(payload))


def test_metadata_manifest_rejects_unsorted_and_duplicate_xattrs():
    unsorted = _payload()
    unsorted["xattrs"] = list(reversed(unsorted["xattrs"]))
    duplicate = _payload()
    duplicate["xattrs"] = [duplicate["xattrs"][0], duplicate["xattrs"][0]]

    with pytest.raises(MetadataManifestError, match="canonical order"):
        decode_metadata_manifest(_encode(unsorted))
    with pytest.raises(MetadataManifestError, match="canonical order"):
        decode_metadata_manifest(_encode(duplicate))


@pytest.mark.parametrize(
    "encoded_value",
    [
        "%%%",
        base64.b64encode(b"value").decode("ascii").rstrip("="),
        "Zh==",
    ],
)
def test_metadata_manifest_rejects_invalid_or_noncanonical_base64(encoded_value):
    payload = _payload()
    payload["xattrs"] = [{"name": "user.value", "value": encoded_value}]

    with pytest.raises(MetadataManifestError):
        decode_metadata_manifest(_encode(payload))


def test_metadata_manifest_rejects_noncanonical_xattr_fields():
    payload = _payload()
    payload["xattrs"] = [
        {
            "name": "user.value",
            "value": base64.b64encode(b"value").decode("ascii"),
            "unexpected": True,
        }
    ]

    with pytest.raises(MetadataManifestError):
        decode_metadata_manifest(_encode(payload))


def test_metadata_manifest_decode_rejects_oversized_input_before_json(monkeypatch):
    calls = []

    def forbidden_loads(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("oversized manifest reached JSON parsing")

    monkeypatch.setattr(
        "mote.runtime.fileops.metadata_manifest.json.loads",
        forbidden_loads,
    )

    with pytest.raises(MetadataManifestError, match="size limit"):
        decode_metadata_manifest(b"x" * (MAX_METADATA_MANIFEST_BYTES + 1))
    assert calls == []


@pytest.mark.parametrize(
    "metadata",
    [
        PreservedMetadata(
            mode=True,
            uid=None,
            gid=None,
            xattrs=(),
            xattrs_supported=True,
        ),
        PreservedMetadata(
            mode=0o600,
            uid=True,
            gid=None,
            xattrs=(),
            xattrs_supported=True,
        ),
        PreservedMetadata(
            mode=0o600,
            uid=None,
            gid=None,
            xattrs=(("user.b", b"b"), ("user.a", b"a")),
            xattrs_supported=True,
        ),
        PreservedMetadata(
            mode=0o600,
            uid=None,
            gid=None,
            xattrs=(("user.a", b"a"), ("user.a", b"b")),
            xattrs_supported=True,
        ),
    ],
)
def test_metadata_manifest_encoder_rejects_noncanonical_metadata(metadata):
    with pytest.raises(MetadataManifestError):
        encode_metadata_manifest(metadata)


def test_metadata_manifest_enforces_xattr_count_name_and_value_limits():
    too_many = PreservedMetadata(
        mode=0o600,
        uid=None,
        gid=None,
        xattrs=tuple((f"user.{index:04d}", b"") for index in range(MAX_METADATA_XATTRS + 1)),
        xattrs_supported=True,
    )
    long_name = PreservedMetadata(
        mode=0o600,
        uid=None,
        gid=None,
        xattrs=(("x" * (MAX_METADATA_XATTR_NAME_BYTES + 1), b""),),
        xattrs_supported=True,
    )
    long_value = PreservedMetadata(
        mode=0o600,
        uid=None,
        gid=None,
        xattrs=(("user.value", b"x" * (MAX_METADATA_XATTR_VALUE_BYTES + 1)),),
        xattrs_supported=True,
    )

    for metadata in (too_many, long_name, long_value):
        with pytest.raises(MetadataManifestError, match="limit"):
            encode_metadata_manifest(metadata)


def test_metadata_manifest_uses_utf8_byte_limit_for_xattr_names():
    within_limit = "a" * (MAX_METADATA_XATTR_NAME_BYTES - 3) + "界"
    over_limit = within_limit + "b"
    common = {
        "mode": 0o600,
        "uid": None,
        "gid": None,
        "xattrs_supported": True,
    }

    encoded = encode_metadata_manifest(PreservedMetadata(xattrs=((within_limit, b""),), **common))
    assert decode_metadata_manifest(encoded).xattrs[0][0] == within_limit
    with pytest.raises(MetadataManifestError, match="size limit"):
        encode_metadata_manifest(PreservedMetadata(xattrs=((over_limit, b""),), **common))
