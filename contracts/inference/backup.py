"""Shared daemon backup consistency contract."""

from enum import StrEnum


class BackupConsistency(StrEnum):
    CRASH_CONSISTENT = "crash_consistent"


__all__ = ["BackupConsistency"]
