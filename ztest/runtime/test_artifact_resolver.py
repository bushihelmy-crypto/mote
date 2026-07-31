from __future__ import annotations

import hashlib

import pytest

from mote.contracts.artifact import (
    ArtifactContentRef,
    ArtifactPublishRequest,
    ArtifactRef,
    ArtifactRepresentationInput,
    ArtifactResolutionPolicy,
    ArtifactSensitivity,
)
from mote.contracts.artifact.errors import ArtifactNotFoundError
from mote.contracts.content import ContentIdentity
from mote.contracts.ports.artifact.store import ArtifactResolver
from mote.runtime.artifacts import DurableArtifactStore, StoreArtifactResolver


class MemoryBlobs:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        digest = hashlib.sha256(content).hexdigest()
        self.contents[digest] = content
        return ArtifactContentRef(
            identity=ContentIdentity(digest, len(content)),
            locator=f"sha256:{digest}",
        )

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        return self.contents[ref.identity.digest]


def policy(
    max_bytes: int = 1024,
    *sensitivities: ArtifactSensitivity,
) -> ArtifactResolutionPolicy:
    return ArtifactResolutionPolicy(
        max_bytes=max_bytes,
        allowed_sensitivities=frozenset(sensitivities or (ArtifactSensitivity.PRIVATE,)),
    )


async def published(tmp_path, content=b"<svg/>", *, sensitivity=ArtifactSensitivity.PRIVATE):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    revision = await store.publish(
        ArtifactPublishRequest(
            sensitivity=sensitivity,
            representations=(
                ArtifactRepresentationInput(
                    representation="svg",
                    kind="canvas",
                    mime_type="image/svg+xml",
                    content=content,
                    suggested_name="diagram.svg",
                ),
            ),
        )
    )
    return store, revision.get("svg")


@pytest.mark.asyncio
async def test_resolver_satisfies_port_and_returns_verified_content(tmp_path):
    store, ref = await published(tmp_path)
    resolver = StoreArtifactResolver(store)

    resolved = await resolver.resolve(ref, policy())

    assert isinstance(resolver, ArtifactResolver)
    assert resolved.ref is ref
    assert resolved.content == b"<svg/>"


@pytest.mark.asyncio
async def test_resolver_rejects_forged_logical_reference(tmp_path):
    store, ref = await published(tmp_path)
    forged = ArtifactRef(
        artifact_id=ref.artifact_id,
        revision=ref.revision,
        representation=ref.representation,
        kind=ref.kind,
        mime_type=ref.mime_type,
        content_ref=ref.content_ref,
        digest="f" * 64,
        size=ref.size,
        retention=ref.retention,
        sensitivity=ref.sensitivity,
        suggested_name=ref.suggested_name,
    )

    with pytest.raises(
        ArtifactNotFoundError,
        match="does not match the durable index",
    ):
        await StoreArtifactResolver(store).resolve(forged, policy())


@pytest.mark.asyncio
async def test_resolver_enforces_size_before_reading():
    digest = hashlib.sha256(b"large").hexdigest()
    ref = ArtifactRef(
        artifact_id="large",
        revision=1,
        representation="bin",
        kind="binary",
        mime_type="application/octet-stream",
        content_ref=f"sha256:{digest}",
        digest=digest,
        size=5,
    )

    class UnexpectedStore:
        async def read(self, _ref):
            raise AssertionError("size policy must fail before store I/O")

    with pytest.raises(ValueError, match="exceeds resolution limit"):
        await StoreArtifactResolver(UnexpectedStore()).resolve(ref, policy(4))


@pytest.mark.asyncio
async def test_resolver_enforces_sensitivity_before_reading(tmp_path):
    store, ref = await published(
        tmp_path,
        sensitivity=ArtifactSensitivity.SECRET,
    )

    with pytest.raises(PermissionError, match="not allowed"):
        await StoreArtifactResolver(store).resolve(ref, policy())


@pytest.mark.asyncio
async def test_resolver_fails_closed_on_store_content_mismatch():
    expected = b"expected"
    digest = hashlib.sha256(expected).hexdigest()
    ref = ArtifactRef(
        artifact_id="mismatch",
        revision=1,
        representation="bin",
        kind="binary",
        mime_type="application/octet-stream",
        content_ref=f"sha256:{digest}",
        digest=digest,
        size=len(expected),
    )

    class CorruptStore:
        async def read(self, _ref):
            return b"corrupt!"

    with pytest.raises(ValueError, match="digest does not match"):
        await StoreArtifactResolver(CorruptStore()).resolve(ref, policy())
