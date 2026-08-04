from __future__ import annotations

import pytest

from mote.contracts.events.scope import ScopeRef, decode_scope_path, encode_scope_path


def test_scope_codec_round_trips_canonical_path() -> None:
    scope = (ScopeRef("graph", "run-1", "pipeline"), ScopeRef("node", "review", "Review"))
    assert decode_scope_path(encode_scope_path(scope)) == scope


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema": "mote.execution-scope/v2", "path": []},
        {"schema": "mote.execution-scope/v1", "path": [], "extra": True},
        {"schema": "mote.execution-scope/v1", "path": [{"kind": "node", "id": "x"}]},
        {"schema": "mote.execution-scope/v1", "path": [{"kind": "", "id": "x", "label": "X"}]},
    ],
)
def test_scope_codec_fails_closed(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_scope_path(payload)


def test_scope_codec_enforces_depth_bound() -> None:
    payload = {
        "schema": "mote.execution-scope/v1",
        "path": [{"kind": "node", "id": str(index), "label": "N"} for index in range(65)],
    }
    with pytest.raises(ValueError, match="path"):
        decode_scope_path(payload)
