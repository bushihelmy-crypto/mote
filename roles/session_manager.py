#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RoleSessionManager — session lifecycle behaviour (resume / fork) for a Role.

Owns session lifecycle behaviour so the Role class stays focused on the run loop
and its thin capability surface. Mirrors :class:`RoleCapabilities` / :class:`RoleStateController`:
this holder owns the *behaviour* that talks to the durable ``session/`` package —
replaying a rollout back into live history, re-seeding the resource registry and
the pending terminal/kernel/browser restores, and branching an independent
sibling role — while the Role keeps thin delegators (``resume_session`` /
``fork_session``) onto these methods.

Holds the Role by reference. ``resume`` mutates the Role's live history + state
(the whole point of a resume); ``fork`` reads the Role and constructs a fresh
sibling of the same class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mote.common.const import RESOURCE_ID, RESOURCE_KIND, RESOURCE_STICKY
from mote.common.events.types import HistoryEditedEvent
from mote.common.interface.event_subscriber import ObservationSubscriber, ObserverPriority
from mote.roles.role_state import RoleState
from mote.session import SessionLog, fork, reconcile_tool_calls, replay

if TYPE_CHECKING:
    from mote.roles.role import Role


class RoleSessionManager:
    """Behaviour behind a Role's session resume / fork lifecycle."""

    def __init__(self, role: "Role"):
        self._role = role

    def resume(self) -> bool:
        """Rebuild the role's stored history from its durable rollout log.

        The rollout (truth source) is replayed into ``state.context.messages``
        for the role's current ``session_id``. Returns False when no log exists
        (nothing to resume). On success the cwd/project anchors are restored from
        the session_meta and ``state.recovered`` is set.

        History is assigned straight into the backing context (not via
        ``ContextManager.add``), so the replayed messages are NOT re-recorded;
        the recorder stays live for messages added after resume, and
        ``SessionLog.create`` no-ops on the existing file (no duplicate meta).

        The in-memory ResourceRegistry is also rebuilt from the replayed history:
        sticky resource messages carry their id/kind/body in metadata, so loaded
        capabilities keep re-projecting after a resume.
        """
        role = self._role
        log = SessionLog(role.state.session_id)
        if not log.exists():
            return False

        result = replay(log)  # replay scans via iter_raw, whose drain flushes queued writes first
        messages = self._reconcile_dangling_calls(result.messages)
        # Assign in place so the ContextManager (which backs onto this same list)
        # sees the rebuilt history without re-recording it.
        role.state.context.messages[:] = messages
        self.reconcile_resources(messages)

        meta = result.meta or {}
        for field_name in ("working_dir", "original_working_dir", "project_root"):
            value = meta.get(field_name)
            if value:
                setattr(role.state, field_name, value)

        role.state.recovered = True
        state_ctl = role._state_ctl
        # Stage the latest persistent-terminal state (if any) so the Terminal
        # tool re-seeds a fresh shell to it on first use — without re-running
        # any of the original commands.
        if result.terminal_state:
            state_ctl.set_pending_terminal_restore(result.terminal_state)
        # Likewise stage the latest persistent-kernel state so the Python tool
        # re-seeds a fresh kernel to it on first use (independent of the shell).
        if result.kernel_state:
            state_ctl.set_pending_kernel_restore(result.kernel_state)
        # Likewise stage the latest persistent-browser state so the WebBrowser
        # tool re-opens the saved tabs (seeded with the stored session) on first
        # use — without re-running any navigation/click actions.
        if result.browser_state:
            state_ctl.set_pending_browser_restore(result.browser_state)
        return True

    def _reconcile_dangling_calls(self, messages):
        """Heal tool calls left dangling by a mid-turn crash, using the ledger.

        A crash between an assistant ``tool_calls`` message being flushed and its
        results being recorded leaves a dangling call that would 400 the next
        provider request. :func:`reconcile_tool_calls` splices a synthetic result
        after each such call — healing it verbatim from the ledger when the effect
        actually completed, or flagging ``<unknown-after-crash>`` when an EXTERNAL
        effect's outcome was lost. After a clean reconcile the resolved records are
        reaped so the ledger does not accumulate across resumes.

        A no-op when the executor has no ledger (feature disabled) — the replayed
        history is returned unchanged.
        """
        ledger = self._role.executor.ledger
        if ledger is None:
            return messages
        outcome = reconcile_tool_calls(messages, ledger)
        if outcome.resolved_ids:
            ledger.reap(outcome.resolved_ids)
        return outcome.messages

    def reconcile_resources(self, messages) -> None:
        """Rebuild the resource side-store to match a freshly-rebuilt history.

        The single seam shared by resume and live history edits (``/clear`` /
        user delete). Both mean "the stored history was structurally replaced, so
        the ResourceRegistry that MIRRORS it must be re-derived from the survivors"
        — identical work, so it lives here once:

          1. :meth:`ResourceRegistry.reset` empties the side-store (a no-op on a
             fresh registry, so resume pays nothing extra for sharing this path).
          2. :meth:`_rebuild_resource_registry` re-seeds sticky capability bodies
             (skills, tool descriptions, …) from the ``RESOURCE_ID`` markers still
             present in ``messages`` — a delete that pruned the message carrying a
             body drops that unit; a survivor keeps it.
          3. :meth:`_rebuild_revealed_tool_resources` re-seeds SPLIT-path tool
             descriptions from ``RoleState.revealed_tools`` (the *durable* reveal
             authority, deliberately history-independent) rather than from history,
             so a reveal survives an edit that pruned its ``SearchTools`` result.

        Distinct from compaction: a ``PostCompactEvent`` condenses the HEAD and
        re-projects sticky bodies through the pull provider, so it must NOT reset
        the registry — only a genuine history *rebuild* routes here.
        """
        self._role.resource_registry.reset()
        self._rebuild_resource_registry(messages)
        self._rebuild_revealed_tool_resources()

    def _rebuild_resource_registry(self, messages) -> None:
        """Re-seed the in-memory ResourceRegistry from replayed history.

        Resource messages survive replay only as metadata (the ``ResourceMessage``
        subclass is lost on dump/load), so we scan every message for the
        ``RESOURCE_ID`` + ``RESOURCE_STICKY`` markers and ``load`` those bodies
        back into the registry. Last-write-wins ordering (a later re-load of the
        same id overwrites the earlier one) falls out of iterating in history
        order. Best-effort: a message without a registry or malformed metadata is
        simply skipped.
        """
        registry = self._role.resource_registry
        for m in messages:
            meta = getattr(m, "metadata", None) or {}
            resource_id = meta.get(RESOURCE_ID)
            if not resource_id or not meta.get(RESOURCE_STICKY):
                continue
            registry.load(
                id=resource_id,
                kind=meta.get(RESOURCE_KIND, "skill"),
                content=m.content or "",
                sticky=True,
            )

    def _rebuild_revealed_tool_resources(self) -> None:
        """Re-seed revealed split-path tool descriptions from ``RoleState``.

        Revealed tools are the SPLIT native path's persisted capability bodies:
        ``SearchTools`` registers each revealed tool's full description as a
        ``kind="tool"`` sticky resource (so a post-compaction summary re-projects
        it). But the durable authority for *which* tools are revealed is
        ``RoleState.revealed_tools`` (not history) — so on resume we rebuild the
        registry units straight from that set + the live catalog descriptions,
        rather than relying on the (non-reconstructable, but still discardable)
        SearchTools result body surviving replay. This keeps the registry
        consistent with the durable reveal set even if the original result was
        compacted away before the crash. Best-effort: no revealed tools, or a
        catalog that no longer describes a name, simply skips it.
        """
        revealed = sorted(self._role.state.revealed_tools)
        if not revealed:
            return
        registry = self._role.resource_registry
        for name, desc in self._role.executor.describe_deferred_tools(revealed).items():
            if desc:
                registry.load(id=name, kind="tool", content=desc, sticky=True)

    def fork(self) -> "Role":
        """Branch a sibling role off this session at its current history.

        Seeds a brand-new ``rollout.jsonl`` from this role's session (replayed to
        its final state) and records ``parent_session_id`` lineage on the child's
        ``session_meta``. Returns a fresh role of the same class, sharing the
        injected context/config, pinned to the new session and resumed onto the
        inherited history. The two sessions are independent afterwards: mutating
        the fork never touches this role's log.
        """
        role = self._role
        child_id = fork(role.state.session_id)

        child_state = RoleState(
            session_id=child_id,
            parent_session_id=role.state.session_id,
            working_dir=role.state.working_dir,
            original_working_dir=role.state.original_working_dir,
            project_root=role.state.project_root,
        )
        forked = type(role)(
            role_schema=role.role_schema.model_copy(deep=True),
            state=child_state,
            context=role._context,
            config=role._config,
        )
        forked.resume_session()
        return forked


class ResourceReconcileSubscriber(ObservationSubscriber):
    """Re-derive the resource side-store when the live history is edited.

    The push half of the same reconciliation :class:`RoleSessionManager` runs on
    resume. A ``/clear`` or user delete emits :class:`HistoryEditedEvent` carrying
    the surviving message list; this observer routes that list straight into
    :meth:`RoleSessionManager.reconcile_resources`, so a body whose message was
    pruned drops out of the registry while revealed-tool descriptions re-seed from
    the durable ``RoleState`` reveal set.

    Deliberately scoped to :class:`HistoryEditedEvent` ONLY — NOT the whole
    :data:`HISTORY_RESET_EVENTS` tuple. A ``PostCompactEvent`` condenses the head
    and re-projects sticky bodies through the ContextManager's pull provider, so
    resetting the registry there would drop units the compaction is trying to keep
    alive; only a genuine live rebuild belongs here. (The turn-context SR sources
    reset on the whole tuple because their frontier IS re-derived every render;
    the registry is not, so its trigger is narrower.)

    Holds the manager by reference (never the raw registry): the manager owns the
    reconciliation behaviour and the revealed-tool authority, keeping this a thin
    bus→behaviour adapter with nothing to drift.
    """

    priority = ObserverPriority.LIVE

    def __init__(self, manager: "RoleSessionManager"):
        self._manager = manager

    async def handle(self, event) -> None:
        if isinstance(event, HistoryEditedEvent):
            self._manager.reconcile_resources(event.messages)
