"""Canonical durable identity and state for one evicted Agent incarnation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, cast

from mote.contracts.content import ContentDigest
from mote.contracts.events.envelope import JsonValue, freeze_json


@dataclass(frozen=True, slots=True)
class ResidencyIdentity:
    logical_agent_id: str
    root_agent_id: str
    parent_agent_id: str | None
    agent_path: str
    nickname: str | None
    definition_id: str
    config_digest: ContentDigest
    incarnation_generation: int

    def __post_init__(self) -> None:
        for name in ("logical_agent_id", "root_agent_id", "agent_path", "definition_id"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"Residency {name} is invalid")
        for name in ("parent_agent_id", "nickname"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"Residency {name} is invalid")
        object.__setattr__(self, "config_digest", ContentDigest(self.config_digest))
        if type(self.incarnation_generation) is not int or self.incarnation_generation < 1:
            raise ValueError("Residency incarnation generation is invalid")


@dataclass(frozen=True, slots=True)
class ResidencyFence:
    subject: str
    owner_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        for name in ("subject", "owner_id"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"Residency fence {name} is invalid")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise ValueError("Residency fencing token is invalid")


class ResidencyLifecycle(StrEnum):
    MATERIALIZED = "materialized"
    INSTALLING = "installing"


@dataclass(frozen=True, slots=True)
class ResidencyRecord:
    identity: ResidencyIdentity
    source_session_revision: int
    record_revision: int
    materialization_fence: ResidencyFence
    state_snapshot: Mapping[str, JsonValue]
    mailbox_snapshot: Mapping[str, JsonValue]
    message_buffer_snapshot: JsonValue
    lifecycle: ResidencyLifecycle = ResidencyLifecycle.MATERIALIZED
    install_fence: ResidencyFence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ResidencyIdentity):
            raise TypeError("Residency record identity is invalid")
        for name in ("source_session_revision", "record_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"Residency {name} is invalid")
        if not isinstance(self.materialization_fence, ResidencyFence):
            raise TypeError("Residency materialization fence is invalid")
        if not isinstance(self.lifecycle, ResidencyLifecycle):
            raise TypeError("Residency lifecycle is invalid")
        if self.lifecycle is ResidencyLifecycle.MATERIALIZED:
            if self.install_fence is not None:
                raise ValueError("materialized Residency record cannot carry an install fence")
        elif not isinstance(self.install_fence, ResidencyFence):
            raise ValueError("installing Residency record requires an install fence")
        state = freeze_json(self.state_snapshot, path="residency.state_snapshot")
        mailbox = freeze_json(self.mailbox_snapshot, path="residency.mailbox_snapshot")
        messages = freeze_json(self.message_buffer_snapshot, path="residency.message_buffer_snapshot")
        if not isinstance(state, Mapping) or not isinstance(mailbox, Mapping):
            raise TypeError("Residency state and mailbox snapshots must be JSON objects")
        object.__setattr__(self, "state_snapshot", cast(Mapping[str, JsonValue], state))
        object.__setattr__(self, "mailbox_snapshot", cast(Mapping[str, JsonValue], mailbox))
        object.__setattr__(self, "message_buffer_snapshot", messages)


__all__ = [
    "ResidencyFence",
    "ResidencyIdentity",
    "ResidencyLifecycle",
    "ResidencyRecord",
]
