"""Shared, immutable query semantics for search and edit planning."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from mote.contracts.fileops.models import PathToken


class RegexPurpose(StrEnum):
    SEARCH = "search"
    EDIT = "edit"


class RegexProgramError(ValueError):
    """The requested regular-expression semantics are invalid."""


@dataclass(frozen=True, slots=True)
class RegexProgram:
    """One compiled regex with the only supported Search/Edit flag policy."""

    pattern: str
    purpose: RegexPurpose
    case_insensitive: bool = False
    dot_matches_newline: bool = False
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.pattern) is not str:
            raise TypeError("regex pattern must be a string")
        if not isinstance(self.purpose, RegexPurpose):
            raise TypeError("regex purpose is invalid")
        if type(self.case_insensitive) is not bool:
            raise TypeError("regex case_insensitive must be a boolean")
        if type(self.dot_matches_newline) is not bool:
            raise TypeError("regex dot_matches_newline must be a boolean")
        try:
            compiled = re.compile(self.pattern, self.flags)
        except re.error as exc:
            raise RegexProgramError(f"invalid regular expression: {exc}") from exc
        if self.purpose == RegexPurpose.EDIT and compiled.search("") is not None:
            raise RegexProgramError("edit regular expressions must not match the empty string")
        object.__setattr__(self, "_compiled", compiled)

    @classmethod
    def for_search(
        cls,
        pattern: str,
        *,
        case_insensitive: bool = False,
        dot_matches_newline: bool = False,
    ) -> RegexProgram:
        return cls(
            pattern=pattern,
            purpose=RegexPurpose.SEARCH,
            case_insensitive=case_insensitive,
            dot_matches_newline=dot_matches_newline,
        )

    @classmethod
    def for_edit(
        cls,
        pattern: str,
        *,
        case_insensitive: bool = False,
        dot_matches_newline: bool = False,
    ) -> RegexProgram:
        return cls(
            pattern=pattern,
            purpose=RegexPurpose.EDIT,
            case_insensitive=case_insensitive,
            dot_matches_newline=dot_matches_newline,
        )

    @property
    def flags(self) -> int:
        flags = re.MULTILINE
        if self.case_insensitive:
            flags |= re.IGNORECASE
        if self.dot_matches_newline:
            flags |= re.DOTALL
        return flags

    def finditer(self, text: str) -> Iterator[re.Match[str]]:
        if type(text) is not str:
            raise TypeError("regex input must be a string")
        for match in self._compiled.finditer(text):
            if self.purpose == RegexPurpose.EDIT and match.start() == match.end():
                raise RegexProgramError("edit regular expressions must not produce empty matches")
            yield match

    def occurrence_count(self, text: str) -> int:
        return sum(1 for _ in self.finditer(text))

    def expand_replacement(self, match: re.Match[str], replacement: str) -> str:
        if self.purpose != RegexPurpose.EDIT:
            raise RegexProgramError("replacement expansion requires an edit regular expression")
        if type(replacement) is not str:
            raise TypeError("regex replacement must be a string")
        if not isinstance(match, re.Match) or match.re is not self._compiled:
            raise RegexProgramError("regex match does not belong to this program")
        return match.expand(replacement)


@dataclass(frozen=True, slots=True)
class CandidateDiscoveryRequest:
    """The complete immutable input to one candidate discovery pass."""

    root: PathToken
    globs: tuple[str, ...] = ()
    type_name: str = ""

    def __post_init__(self) -> None:
        _validate_path_token(self.root, field_name="candidate discovery root")
        if type(self.globs) is not tuple:
            raise TypeError("candidate discovery globs must be a tuple")
        for pattern in self.globs:
            if type(pattern) is not str or not pattern or "\x00" in pattern:
                raise ValueError("candidate discovery glob is invalid")
        canonical_globs = tuple(sorted(set(self.globs)))
        if canonical_globs != self.globs:
            object.__setattr__(self, "globs", canonical_globs)
        if type(self.type_name) is not str or "\x00" in self.type_name:
            raise ValueError("candidate discovery type name is invalid")


@dataclass(frozen=True, slots=True)
class CandidateDiscovery:
    """A frozen, canonically ordered candidate set with no content access."""

    request: CandidateDiscoveryRequest
    candidates: tuple[PathToken, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, CandidateDiscoveryRequest):
            raise TypeError("candidate discovery request is invalid")
        if type(self.candidates) is not tuple:
            raise TypeError("candidate discovery candidates must be a tuple")
        keyed: list[tuple[bytes, PathToken]] = []
        identities: set[bytes] = set()
        for candidate in self.candidates:
            _validate_path_token(candidate, field_name="candidate")
            identity = _candidate_identity(candidate)
            if identity in identities:
                raise ValueError("candidate discovery contains a duplicate path")
            identities.add(identity)
            keyed.append((identity, candidate))
        canonical = tuple(candidate for _, candidate in sorted(keyed, key=lambda x: x[0]))
        if canonical != self.candidates:
            object.__setattr__(self, "candidates", canonical)

    @classmethod
    def freeze(
        cls,
        request: CandidateDiscoveryRequest,
        candidates: Iterable[PathToken],
    ) -> CandidateDiscovery:
        return cls(request=request, candidates=tuple(candidates))


def _validate_path_token(path: PathToken, *, field_name: str) -> None:
    if not isinstance(path, PathToken):
        raise TypeError(f"{field_name} must be a PathToken")
    if type(path.native) not in (str, bytes) or not path.native:
        raise ValueError(f"{field_name} native path is invalid")
    if not os.path.isabs(path.native):
        raise ValueError(f"{field_name} native path must be absolute")
    if type(path.display) is not str or path.display != os.fsdecode(path.native):
        raise ValueError(f"{field_name} display path is not lossless")


def _candidate_identity(path: PathToken) -> bytes:
    normalized = os.path.normcase(os.path.normpath(path.native))
    return os.fsencode(normalized)


__all__ = [
    "CandidateDiscovery",
    "CandidateDiscoveryRequest",
    "RegexProgram",
    "RegexProgramError",
    "RegexPurpose",
]
