"""TeamContextSource — the multi-agent lineage this agent sits in.

Surfaces the agent's immediate neighbourhood in the session tree — its parent,
its siblings, and its direct (one-generation) children — together with each
teammate's **session id**, so the model can address a specific agent by id when
it delegates, replies, or queries a teammate.

Sourced live from the control plane (``AgentControl``): the roster is read from
the registry, which is the tree the ``environment`` layer grows as agents spawn.

**Incremental + persisted**, mirroring the tool-catalogue / skills-index feeds:
a teammate's session id is a durable reference the model keeps consulting, and
the roster only ever *grows* (a child or sibling is added), so this rides
history rather than the volatile tail. Each turn it diffs the live roster
against a frontier of already-announced session ids and emits **only the newly
appeared teammates** — the first turn carries the full roster (parent +
pre-existing siblings/children), and thereafter each spawn surfaces once as an
additive block. Already-announced members are never repeated (the base lives in
history). Any :data:`MODEL_CONTEXT_REBUILT_EVENTS` (a compaction *or* a ``/clear`` / user
delete that rebuilds stored history) resets the frontier so the full live roster
is re-emitted after the earlier announcement was condensed or pruned away.

Layer-clean: it receives a narrow roster provider at composition time and never
imports the Agent or orchestration layers. When no provider is installed (a
plane-less single agent / tests) or there is nothing new to announce, it
self-suppresses by returning ``None``.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Set

from mote.contracts.events.conversation import MODEL_CONTEXT_REBUILT_EVENTS
from mote.contracts.ports.agent.team_roster import TeamRosterMember, TeamRosterProvider
from mote.contracts.ports.conversation.turn_context import TurnContextPriority
from mote.runtime.telemetry.logging import logger

RosterProvider = Callable[[], Optional[TeamRosterProvider]]
#: Returns this agent's own ``session_id`` (its registry key), or ``None``.
SessionIdProvider = Callable[[], Optional[str]]


class TeamContextSource:
    """Announces newly-appeared parent / siblings / children (with session ids).

    Its explicit post-commit rebuild callback resets the frontier on any
    :data:`MODEL_CONTEXT_REBUILT_EVENTS`; its ephemeral-context surface renders
    the roster delta each turn.
    """

    name = "team"
    # Structural orientation, right after the repo snapshot: who this agent can
    # talk to sits alongside where it is working.
    priority = TurnContextPriority.TEAM
    # Persisted + incremental: session ids are durable references and the roster
    # only grows, so the delta rides history (re-emitted in full after a
    # compaction condenses the earlier announcement away).
    save_to_context = True

    def __init__(
        self,
        get_session_id: SessionIdProvider,
        get_provider: Optional[RosterProvider] = None,
    ) -> None:
        self._get_session_id = get_session_id
        self._get_provider = get_provider
        # Frontier of teammate session ids already announced into history.
        self._sent_ids: Set[str] = set()

    async def on_model_context_rebuilt(self, event: object) -> None:
        """Reset the frontier whenever stored history is structurally rebuilt.

        A compaction condenses the earlier roster announcement away; a ``/clear``
        or user delete prunes the messages that carried it. Both fold to
        ``MODEL_CONTEXT_REBUILT_EVENTS`` — clearing the frontier makes the next render
        re-diff against the live roster and re-emit the still-present teammates.
        """
        if isinstance(event, MODEL_CONTEXT_REBUILT_EVENTS):
            self._sent_ids.clear()

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        try:
            provider = self._get_provider() if self._get_provider is not None else None
            if provider is None:
                return None
            session_id = self._get_session_id()
            if not session_id:
                return None
            members = provider.team_members(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort; never break a turn
            logger.warning(f"team snapshot failed: {exc}")
            return None
        # Emit only teammates not yet announced (the base already lives in
        # history); advance the frontier over the survivors.
        fresh = [m for m in members if m.session_id and m.session_id not in self._sent_ids]
        if not fresh:
            return None
        self._sent_ids.update(m.session_id for m in fresh)
        return _format(fresh)


def _format(members: List[TeamRosterMember]) -> str:
    """Render the roster delta, grouped parent → siblings → children."""
    parent = next((m for m in members if m.relation == "parent"), None)
    siblings = [m for m in members if m.relation == "sibling"]
    children = [m for m in members if m.relation == "child"]

    lines = [
        "# Team",
        "Your place in the agent tree (address a teammate by its session id):",
    ]
    if parent is not None:
        lines.append(f"Parent: {_line(parent)}")
    if siblings:
        lines.append("Siblings:")
        lines.extend(f"- {_line(m)}" for m in siblings)
    if children:
        lines.append("Children:")
        lines.extend(f"- {_line(m)}" for m in children)
    return "\n".join(lines)


def _line(member: TeamRosterMember) -> str:
    """One teammate line: ``name (role=…, session=…, status=…)``."""
    facets = [f"session={member.session_id}"]
    if member.role:
        facets.insert(0, f"role={member.role}")
    if member.status:
        facets.append(f"status={member.status}")
    label = member.name or member.session_id
    return f"{label} ({', '.join(facets)})"


__all__ = ["TeamContextSource"]
