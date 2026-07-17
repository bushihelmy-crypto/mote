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
history). Any :data:`HISTORY_RESET_EVENTS` (a compaction *or* a ``/clear`` / user
delete that rebuilds stored history) resets the frontier so the full live roster
is re-emitted after the earlier announcement was condensed or pruned away.

Layer-clean: it reaches the plane through the ambient discovery surface
(:func:`resolve_control`) and walks the registry entirely by duck-typing, so the
low ``context`` layer never imports ``environment``. When no plane is bound (a
plane-less single agent / tests) or there is nothing new to announce, it
self-suppresses by returning ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Set

from mote.common.agent_control import resolve_control
from mote.common.events import HISTORY_RESET_EVENTS
from mote.common.interface import ObservationSubscriber, TurnContextPriority
from mote.common.logs import logger

#: Returns the LLM ``Context`` (which may carry an explicit ``agent_control``),
#: or ``None``. ``resolve_control`` prefers it over the ambient plane.
ContextProvider = Callable[[], Any]
#: Returns this agent's own ``session_id`` (its registry key), or ``None``.
SessionIdProvider = Callable[[], Optional[str]]


@dataclass
class TeamMember:
    """One teammate in the agent's immediate lineage neighbourhood."""

    relation: str  # "parent" | "sibling" | "child"
    name: str  # nickname (falls back to the path's last segment)
    role: str  # the teammate's role/type ("" when unknown)
    session_id: str  # the teammate's session id — how to address it
    status: str  # live status ("running" / "idle" / ... ; "" when unknown)


class TeamContextSource(ObservationSubscriber):
    """Announces newly-appeared parent / siblings / children (with session ids).

    Push→pull in one object (like ``GitContextSource``): as an
    :class:`ObservationSubscriber` it resets its frontier on any
    :data:`HISTORY_RESET_EVENTS`; as an ephemeral-context source it renders the
    roster delta each turn.
    """

    name = "team"
    # Structural orientation, right after the repo snapshot: who this agent can
    # talk to sits alongside where it is working.
    priority = TurnContextPriority.TEAM
    # Persisted + incremental: session ids are durable references and the roster
    # only grows, so the delta rides history (re-emitted in full after a
    # compaction condenses the earlier announcement away).
    save_to_context = True

    def __init__(self, get_session_id: SessionIdProvider, get_context: Optional[ContextProvider] = None) -> None:
        self._get_session_id = get_session_id
        self._get_context = get_context
        # Frontier of teammate session ids already announced into history.
        self._sent_ids: Set[str] = set()

    async def handle(self, event) -> None:
        """Reset the frontier whenever stored history is structurally rebuilt.

        A compaction condenses the earlier roster announcement away; a ``/clear``
        or user delete prunes the messages that carried it. Both fold to
        ``HISTORY_RESET_EVENTS`` — clearing the frontier makes the next render
        re-diff against the live roster and re-emit the still-present teammates.
        """
        if isinstance(event, HISTORY_RESET_EVENTS):
            self._sent_ids.clear()
        return None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        try:
            ctx = self._get_context() if self._get_context is not None else None
            control = resolve_control(ctx)
            if control is None:
                return None
            session_id = self._get_session_id()
            if not session_id:
                return None
            members = _collect_team(control, session_id)
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


def _collect_team(control: Any, session_id: str) -> List[TeamMember]:
    """Walk the registry for *session_id*'s parent, siblings, and direct children.

    Entirely duck-typed against the control plane's registry so the ``context``
    layer stays free of any ``environment`` import: it only calls
    ``agent_metadata_for_id`` / ``agent_id_for_path`` / ``live_agents`` and reads
    ``agent_path`` (``parent`` / ``name`` / ``__eq__``) off the returned metadata.
    """
    registry = getattr(control, "registry", None)
    if registry is None:
        return []
    own = registry.agent_metadata_for_id(session_id)
    if own is None or own.agent_path is None:
        return []
    own_path = own.agent_path
    parent_path = own_path.parent()

    members: List[TeamMember] = []
    # Parent — resolved by path so a root parent (excluded from live_agents) is
    # still found.
    if parent_path is not None:
        parent_id = registry.agent_id_for_path(parent_path)
        if parent_id:
            parent_meta = registry.agent_metadata_for_id(parent_id)
            if parent_meta is not None:
                members.append(_member("parent", parent_meta, control))

    # Siblings (share this agent's parent) + direct children (this agent is their
    # parent), both drawn from the live, non-root agent set in one pass.
    for meta in registry.live_agents():
        if meta.agent_id == session_id or meta.agent_path is None:
            continue
        meta_parent = meta.agent_path.parent()
        if parent_path is not None and meta_parent == parent_path:
            members.append(_member("sibling", meta, control))
        elif meta_parent == own_path:
            members.append(_member("child", meta, control))
    return members


def _member(relation: str, meta: Any, control: Any) -> TeamMember:
    """Build one :class:`TeamMember` from registry metadata + live status."""
    session_id = meta.agent_id or ""
    status = ""
    if session_id:
        try:
            status = control.get_status(session_id).value
        except Exception:  # noqa: BLE001 — status is best-effort decoration
            status = ""
    name = meta.agent_nickname or (meta.agent_path.name() if meta.agent_path is not None else "")
    return TeamMember(
        relation=relation,
        name=name,
        role=meta.agent_role or "",
        session_id=session_id,
        status=status,
    )


def _format(members: List[TeamMember]) -> str:
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


def _line(member: TeamMember) -> str:
    """One teammate line: ``name (role=…, session=…, status=…)``."""
    facets = [f"session={member.session_id}"]
    if member.role:
        facets.insert(0, f"role={member.role}")
    if member.status:
        facets.append(f"status={member.status}")
    label = member.name or member.session_id
    return f"{label} ({', '.join(facets)})"


__all__ = ["TeamContextSource", "TeamMember"]
