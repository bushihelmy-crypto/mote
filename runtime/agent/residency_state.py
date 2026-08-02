"""Runtime adapter between Role state/config and the Residency ports."""

from __future__ import annotations

import hashlib
import json
from typing import Mapping, cast

from pydantic import BaseModel

from mote.contracts.content import ContentDigest
from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json


def residency_config_digest(
    *,
    definition_id: str,
    role_schema: BaseModel,
    runtime_config: BaseModel | None,
) -> ContentDigest:
    if type(definition_id) is not str or not definition_id:
        raise ValueError("Residency definition identity is invalid")
    payload = freeze_json(
        {
            "definition_id": definition_id,
            "role_schema": role_schema.model_dump(mode="json"),
            "runtime_config": (None if runtime_config is None else runtime_config.model_dump(mode="json")),
        },
        path="residency.config",
    )
    encoded = json.dumps(thaw_json(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ContentDigest(hashlib.sha256(encoded).hexdigest())


def freeze_state(value: object) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value, path="residency.state")
    if not isinstance(frozen, Mapping):
        raise TypeError("Residency state must be a JSON object")
    return cast(Mapping[str, JsonValue], frozen)


__all__ = ["freeze_state", "residency_config_digest"]
