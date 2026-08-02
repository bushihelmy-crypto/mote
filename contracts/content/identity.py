"""Repository-neutral content-addressed identity values."""

import re
from dataclasses import dataclass
from enum import StrEnum


class DigestAlgorithm(StrEnum):
    SHA256 = "sha256"


class ContentDigest(str):
    algorithm = DigestAlgorithm.SHA256

    def __new__(cls, value: str):
        normalized = value.lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
            raise ValueError("content digest must be lowercase SHA-256 hex")
        return str.__new__(cls, normalized)


@dataclass(frozen=True, slots=True)
class ContentIdentity:
    digest: ContentDigest
    size: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", ContentDigest(self.digest))
        if type(self.size) is not int or not 0 <= self.size < (1 << 63):
            raise ValueError("content size must be an unsigned 63-bit integer")


__all__ = ["ContentDigest", "ContentIdentity", "DigestAlgorithm"]
