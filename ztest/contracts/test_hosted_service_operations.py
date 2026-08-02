"""Strict operation-tagged contracts for Product hosted services."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.service import (
    MediaGenerationPayload,
    MediaGenerationSpec,
    MediaKind,
    ServiceExecutionSemantics,
    ServiceInvocation,
    WebSearchPayload,
)


def _invocation(payload, capability: str) -> ServiceInvocation:
    return ServiceInvocation(
        service_call_id="call-1",
        route_id="route-1",
        capability=capability,
        payload=payload,
        semantics=ServiceExecutionSemantics.PURE,
        idempotency_key="key-1",
    )


def test_service_invocation_rejects_unknown_or_unversioned_payload_shape() -> None:
    with pytest.raises(ValidationError, match="union_tag_not_found"):
        _invocation({"query": "python"}, "web.search")
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        _invocation({"kind": "future", "query": "python"}, "web.search")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        WebSearchPayload.model_validate({"query": "python", "unexpected": True})


def test_service_invocation_binds_capability_to_payload_tag() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        _invocation(WebSearchPayload(query="python"), "media.generate.image")


def test_media_payload_is_kind_aware_and_strict() -> None:
    with pytest.raises(ValidationError, match="audio generation requires text"):
        MediaGenerationPayload(
            media_kind=MediaKind.AUDIO,
            item=MediaGenerationSpec(prompt="not speech"),
        )
    with pytest.raises(ValidationError, match="int_type"):
        MediaGenerationSpec.model_validate({"prompt": "clip", "seconds": True})

    payload = MediaGenerationPayload(
        media_kind=MediaKind.VIDEO,
        item=MediaGenerationSpec(prompt="waves", filename="waves.mp4", seconds=4),
    )
    assert _invocation(payload, "media.generate.video").payload is payload
