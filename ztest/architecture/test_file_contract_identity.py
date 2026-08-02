from __future__ import annotations

from typing import get_type_hints

import pytest

from mote.contracts.content.identity import ContentDigest, ContentIdentity
from mote.contracts.file.codec import blob_from_dict, blob_to_dict
from mote.contracts.file.search import SearchResult


def test_search_result_type_hints_resolve_authoritative_content_identity() -> None:
    hints = get_type_hints(SearchResult)

    assert hints["artifact"] is ContentIdentity
    assert hints["skipped_artifact"] is ContentIdentity


def test_blob_codec_preserves_nominal_digest_and_bytes_semantics() -> None:
    digest = ContentDigest("ab" * 32)
    original = ContentIdentity(digest=digest, size=17)

    decoded = blob_from_dict(blob_to_dict(original))

    assert decoded == original
    assert decoded is not None
    assert type(decoded.digest) is ContentDigest
    assert bytes.fromhex(decoded.digest) == bytes.fromhex(digest)


@pytest.mark.parametrize("size", [-1, True, 1 << 63])
def test_content_identity_rejects_sizes_its_wire_codec_cannot_replay(size: object) -> None:
    with pytest.raises(ValueError, match="63-bit"):
        ContentIdentity(digest=ContentDigest("ab" * 32), size=size)  # type: ignore[arg-type]


@pytest.mark.parametrize("digest", ["AB" * 32, "a" * 63, "g" * 64])
def test_blob_codec_rejects_noncanonical_digest(digest: str) -> None:
    with pytest.raises(ValueError, match="digest is invalid"):
        blob_from_dict({"digest": digest, "size": 1})
