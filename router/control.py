#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Router-control holds — ported from opensquilla's ``router_control.py``.

A *hold* pins routing to an operator-chosen model for a session, expiring after
an idle TTL (default 600s) or an optional turn-count budget. This is how a user
can say "stick with the strong model for now" and have it honoured across turns
until it lapses. opensquilla derived control targets from its router-config
tiers; here they are derived from the router's registered :class:`ModelCard`
set (name + tier), so the menu always matches what the router can actually build.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from metagpt.router.schema import ModelCard

# Zero turns means no turn-count cap; the hold expires on idle TTL only.
DEFAULT_HOLD_TURNS = 0
DEFAULT_HOLD_TTL_SECONDS = 600.0


class RouterControlValidationError(ValueError):
    """Raised when a control target id is not among the registered models."""


@dataclass(frozen=True)
class RouterControlTarget:
    target_id: str
    name: str
    tier: int
    description: Optional[str] = None


@dataclass
class RouterControlHold:
    name: str
    tier: int
    target_id: str
    evidence: str
    started_at_monotonic: float
    last_activity_at_monotonic: Optional[float] = None
    turns_remaining: int = DEFAULT_HOLD_TURNS
    ttl_seconds: float = DEFAULT_HOLD_TTL_SECONDS
    source: str = "router_control"

    def is_expired(self, now_monotonic: float) -> tuple[bool, Optional[str]]:
        if self.turns_remaining < 0:
            return True, "turn_count"
        last_activity = self.last_activity_at_monotonic
        if last_activity is None:
            last_activity = self.started_at_monotonic
        if now_monotonic - last_activity >= self.ttl_seconds:
            return True, "ttl"
        return False, None


def build_control_targets(cards: dict[str, ModelCard]) -> list[RouterControlTarget]:
    """Canonical control targets derived from the registered model cards."""
    return [
        RouterControlTarget(
            target_id=f"model:{name}",
            name=name,
            tier=card.tier,
            description=card.description or None,
        )
        for name, card in cards.items()
    ]


def resolve_control_target(cards: dict[str, ModelCard], target_id: str) -> RouterControlTarget:
    normalized = str(target_id or "").strip()
    if not normalized:
        raise RouterControlValidationError("router_control target_id is required")
    # accept either "model:<name>" or a bare model name
    targets = {t.target_id: t for t in build_control_targets(cards)}
    if normalized in targets:
        return targets[normalized]
    by_name = {t.name: t for t in targets.values()}
    if normalized in by_name:
        return by_name[normalized]
    raise RouterControlValidationError(f"router_control target_id {normalized!r} is not registered")


class RouterControlHoldStore:
    """Short-lived in-memory router-control holds keyed by session key."""

    def __init__(self) -> None:
        self._holds: dict[str, RouterControlHold] = {}

    def set_hold(
        self,
        session_key: str,
        target: RouterControlTarget,
        *,
        evidence: str = "",
        now_monotonic: Optional[float] = None,
        turns_remaining: int = DEFAULT_HOLD_TURNS,
        ttl_seconds: float = DEFAULT_HOLD_TTL_SECONDS,
    ) -> RouterControlHold:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        hold = RouterControlHold(
            name=target.name,
            tier=target.tier,
            target_id=target.target_id,
            evidence=str(evidence or "").strip(),
            started_at_monotonic=now,
            last_activity_at_monotonic=now,
            turns_remaining=turns_remaining,
            ttl_seconds=ttl_seconds,
        )
        self._holds[session_key] = hold
        return hold

    def clear(self, session_key: str) -> Optional[RouterControlHold]:
        return self._holds.pop(session_key, None)

    def get_valid(
        self,
        session_key: str,
        *,
        now_monotonic: Optional[float] = None,
        decrement: bool = False,
    ) -> Optional[RouterControlHold]:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        hold = self._holds.get(session_key)
        if hold is None:
            return None
        expired, _reason = hold.is_expired(now)
        if expired:
            self._holds.pop(session_key, None)
            return None
        if decrement:
            hold.last_activity_at_monotonic = now
            had_turn_limit = hold.turns_remaining > 0
            if hold.turns_remaining > 0:
                hold.turns_remaining -= 1
            if hold.turns_remaining < 0 or (had_turn_limit and hold.turns_remaining == 0):
                self._holds.pop(session_key, None)
        return hold
