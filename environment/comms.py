#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CommGraph — the communication graph, a first-class control-plane subsystem.

The lineage tree (``AgentRegistry``) answers *who spawned whom*; the communication
graph answers *who can talk to whom*. They are **orthogonal**: an agent's place in
the spawn tree says nothing about which named channels it listens on or which
addresses route to it. This module owns the routing state that used to live
ad-hoc inside ``AgentEnvironment``:

  * **addresses** — ``session_id -> {address}`` index (codex address map). A
    message's ``send_to`` set is matched against these to find recipients.
  * **channels** — named broadcast groups (``channel -> {session_id}``). Agents
    join/leave channels; :meth:`send_to_channel` (on the control plane) fans a
    message out to every member.
  * **paths** — ``session_id -> AgentPath`` so :meth:`subtree_members` can answer
    "every agent at or below this path" for ``broadcast_subtree``.

The graph is pure bookkeeping: it never holds runtimes and never decides
liveness. Broadcast-to-all resolution is handed the live id set by the caller
(the control plane), keeping this module free of any runtime dependency.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Iterable, List, Optional, Set

from metagpt.common.const import MESSAGE_ROUTE_TO_ALL
from metagpt.environment.agent_path import AgentPath


class CommKind(str, Enum):
    """The semantic kind of an inter-agent message.

    Orthogonal to :class:`~metagpt.environment.mailbox.DeliveryMode` (which only
    says *whether to wake a turn*): the kind describes *what the message means* so
    a recipient (or an observer) can tell a delegated task from a result, a
    notification, or a query.
    """

    TASK = "task"
    NOTIFICATION = "notification"
    RESULT = "result"
    QUERY = "query"
    BROADCAST = "broadcast"


class CommGraph:
    """Address routing + named channels + subtree queries (the comm plane)."""

    def __init__(self):
        self._addresses: Dict[str, Set[str]] = {}  # session_id -> addresses
        self._channels: Dict[str, Set[str]] = {}  # channel -> session_ids
        self._paths: Dict[str, AgentPath] = {}  # session_id -> path

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    def register(
        self,
        session_id: str,
        *,
        addresses: Optional[Iterable[str]] = None,
        agent_path: Optional[AgentPath] = None,
    ) -> None:
        """Register an agent's routing facets (addresses and/or path)."""
        if addresses is not None:
            self._addresses[session_id] = set(addresses)
        if agent_path is not None:
            self._paths[session_id] = agent_path

    def set_addresses(self, session_id: str, addresses: Optional[Iterable[str]]) -> None:
        """Replace the address set routing to *session_id*."""
        self._addresses[session_id] = set(addresses or [])

    def addresses_for(self, session_id: str) -> Set[str]:
        return set(self._addresses.get(session_id, set()))

    def path_for(self, session_id: str) -> Optional[AgentPath]:
        return self._paths.get(session_id)

    def remove(self, session_id: str) -> None:
        """Forget an agent entirely (addresses, path, and channel memberships)."""
        self._addresses.pop(session_id, None)
        self._paths.pop(session_id, None)
        for members in self._channels.values():
            members.discard(session_id)

    # ------------------------------------------------------------------
    # Named channels
    # ------------------------------------------------------------------
    def join_channel(self, channel: str, session_id: str) -> None:
        self._channels.setdefault(channel, set()).add(session_id)

    def leave_channel(self, channel: str, session_id: str) -> None:
        members = self._channels.get(channel)
        if members is not None:
            members.discard(session_id)
            if not members:
                del self._channels[channel]

    def channel_members(self, channel: str) -> List[str]:
        return sorted(self._channels.get(channel, set()))

    def channels(self) -> List[str]:
        return sorted(self._channels.keys())

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def resolve_recipients(self, send_to: Set[str], *, all_ids: Optional[Iterable[str]] = None) -> List[str]:
        """Map a message's ``send_to`` address set to recipient session ids.

        ``MESSAGE_ROUTE_TO_ALL`` resolves to the caller-supplied ``all_ids`` (the
        live agent set — the comm graph does not track liveness). Any other
        address set is matched against each agent's address index.
        """
        if MESSAGE_ROUTE_TO_ALL in send_to:
            return list(all_ids or [])
        recipients = []
        for session_id, addresses in self._addresses.items():
            if addresses & send_to:
                recipients.append(session_id)
        return recipients

    def subtree_members(self, root_path: AgentPath, *, include_root: bool = True) -> List[str]:
        """Every registered agent at or below *root_path* (lineage subtree)."""
        root_str = root_path.as_str()
        prefix = root_str + "/"
        out = []
        for session_id, path in self._paths.items():
            path_str = path.as_str()
            if path_str == root_str:
                if include_root:
                    out.append(session_id)
            elif path_str.startswith(prefix):
                out.append(session_id)
        return out


__all__ = ["CommGraph", "CommKind"]
