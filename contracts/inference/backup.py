"""Shared daemon/application backup barrier contracts."""

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class BackupConsistency(StrEnum):
    APPLICATION_CONSISTENT = "application_consistent"
    DAEMON_CONSISTENT = "daemon_consistent"
    CRASH_CONSISTENT = "crash_consistent"


class BackupBarrierCut(FrozenContract):
    schema_version: Literal[1] = 1
    backup_id: str = Field(min_length=1, max_length=256)
    backup_epoch: int = Field(ge=1)
    admission_epoch: int = Field(ge=1)
    required_participants: tuple[str, ...] = Field(min_length=1)
    acknowledged_participants: tuple[str, ...] = ()
    daemon_checkpoint_verified: bool
    component_digests_verified: bool

    @model_validator(mode="after")
    def _validate_participants(self) -> "BackupBarrierCut":
        required = set(self.required_participants)
        acknowledged = set(self.acknowledged_participants)
        if len(required) != len(self.required_participants):
            raise ValueError("required backup participants must be unique")
        if len(acknowledged) != len(self.acknowledged_participants):
            raise ValueError("acknowledged backup participants must be unique")
        if not acknowledged <= required:
            raise ValueError("unknown caller acknowledged backup barrier")
        return self

    @property
    def missing_participants(self) -> tuple[str, ...]:
        acknowledged = set(self.acknowledged_participants)
        return tuple(participant for participant in self.required_participants if participant not in acknowledged)


__all__ = ["BackupBarrierCut", "BackupConsistency"]
