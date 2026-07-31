"""Stable command protocol vocabulary."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProtocolVocabulary:
    protocol: str
    version: str
    symbols: tuple[tuple[str, str], ...]
    fingerprint: str


__all__ = ["ProtocolVocabulary"]
