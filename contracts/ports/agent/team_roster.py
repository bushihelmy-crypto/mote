"""Port for rendering an Agent's immediate team neighbourhood."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class TeamRosterMember:
    relation: str
    name: str
    role: str
    session_id: str
    status: str


class TeamRosterProvider(Protocol):
    def team_members(self, session_id: str) -> Sequence[TeamRosterMember]:
        ...


__all__ = ["TeamRosterMember", "TeamRosterProvider"]
