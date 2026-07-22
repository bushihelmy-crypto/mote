#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleComponents — declarative assembly + ownership of a Role's subsystems.

The Role is pure orchestration; the wiring of its (mostly opt-in, lazily built)
collaborators — the LLM router, tool executor, context manager, event bus,
session log, hook/LSP/file-watch services, the per-turn context bus, etc. —
lives here. The Role keeps the public property surface (``role.executor``,
``role.event_bus`` …) as thin delegators onto this object, so external callers
and tests are unchanged.

Each collaborator is declared once as a :class:`ComponentSpec` in
:meth:`RoleComponents._component_specs` and resolved by a single lazy,
cycle-checked :class:`ComponentGraph`. A builder reads its siblings only through
``ctx.dep`` (eager, cycle-tracked) / ``ctx.defer`` (a lazy thunk), and role facts
through ``ctx.role`` / ``ctx.state`` — so *construction* is a pure DAG (a build
cycle raises :class:`ComponentCycleError` at the traversal, never a stack
overflow) and no builder mutates a sibling. The two genuinely cyclic runtime
cross-references (the event spine ⇄ its subscribers, and the router ← context
manager reducer edge) are layered on *afterward* by explicit lifecycle steps
(:meth:`_wire_spine` / :meth:`_wire_collaborators`), driven from
``Role._ensure_ready`` before the first turn.

Mutable extras that are not themselves components (queued Python hook callbacks,
the pending task-completion wake, the live resource-cap guard) live on
:class:`ComponentsState`, threaded to builders via ``ctx.state``. ``peek_*``
accessors expose a built slot without triggering a build (teardown paths).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from mote.common.const import MOTE_REPORTER_DEFAULT_URL
from mote.common.events import EventBus, LogSubscriber
from mote.common.interface import ObservationSubscriber
from mote.common.observability.langfuse_backend import LangfuseBackend
from mote.common.observability.langfuse_integration import is_enabled, step_tracing_enabled
from mote.common.observability.tracing import TracingSubscriber
from mote.common.utils.report import ReporterSubscriber
from mote.roles.capabilities import RoleCapabilities
from mote.roles.component_accessors import RoleComponentAccessors
from mote.roles.component_graph import ComponentGraph, ComponentSpec
from mote.roles.role_state import RoleStateController
from mote.roles.runtime_maintenance import RuntimeMaintenance
from mote.roles.runtime_modules import (
    WatchingCallbacks,
    action_component_specs,
    cognition_component_specs,
    context_component_specs,
    integration_component_specs,
    integration_event_subscribers,
    session_component_specs,
    session_event_subscribers,
    watching_component_specs,
)
from mote.roles.session_manager import ResourceReconcileSubscriber, RoleSessionManager

if TYPE_CHECKING:
    from mote.roles.role import Role


class ComponentsState:
    """Mutable per-Role extras that are not themselves graph components.

    Threaded into every builder via ``ctx.state`` so a spec can read/adjust them
    without reaching for a sibling: the queued Python hook callbacks (seeded into
    the HookManager on build), the pending task-completion wake (stashed before
    the pool exists), and the live resource-cap guard (set by the sandbox builder,
    read by ``peek_resource_guard``).
    """

    def __init__(self) -> None:
        self.pending_task_completion_wake: "Optional[Callable]" = None
        self.hook_callbacks: list[tuple[str, Any, Optional[str]]] = []
        self.resource_guard: Optional[ResourceGuard] = None


class RoleComponents(RoleComponentAccessors):
    """Owns and lazily builds the Role's collaborators (see module docstring)."""

    def __init__(self, role: "Role"):
        self._role = role
        self._state = ComponentsState()
        self._maintenance = RuntimeMaintenance(
            role,
            get=lambda name: self._graph.get(name),
            peek=lambda name: self._graph.peek(name),
        )
        self._graph = ComponentGraph(role, self._state, self._component_specs())
        # The spine is wired exactly once, as an explicit lifecycle step
        # (``_wire_spine`` from ``Role._ensure_ready``), never as a side-effect of
        # constructing the leaf ``event_bus``. Set True *before* the wiring body
        # runs so a redundant/re-entrant call short-circuits (idempotent).
        self._spine_wired = False
        # Runtime cross-references between already-built collaborators (e.g. the
        # router ← ContextManager reducer edge), established once as an explicit
        # lifecycle step (``_wire_collaborators`` from ``Role._ensure_ready``) so
        # no getter mutates a *sibling* component as a hidden read side-effect.
        self._collaborators_wired = False

    # =========================================================================
    # Declarative component registry
    # =========================================================================

    def _component_specs(self) -> list[ComponentSpec]:
        """The single declarative list of every buildable collaborator.

        One entry per component: its name (= the Role's public attribute), a pure
        ``build`` of a ``BuildContext``, and an optional ``available`` gate for the
        opt-in layers. Read top-to-bottom, this is the Role's whole subsystem
        graph as data; the resolver (:class:`ComponentGraph`) turns it into lazy,
        cycle-checked access.
        """
        return [
            # --- leaves (read no sibling) -----------------------------------
            # Behaviour holders over the Role: the state controller reads only the
            # (pure-DTO) RoleState, the capabilities holder only the Role itself.
            # Registered as leaves so the Role's ``_state_ctl`` / ``_capabilities``
            # delegators resolve them through the one graph like every other
            # collaborator (Role.__init__ builds nothing but this holder).
            ComponentSpec("state_ctl", lambda ctx: RoleStateController(ctx.role.state)),
            ComponentSpec("capabilities", lambda ctx: RoleCapabilities(ctx.role)),
            ComponentSpec("session_manager", lambda ctx: RoleSessionManager(ctx.role)),
            *action_component_specs(),
            *session_component_specs(),
            ComponentSpec("event_bus", lambda ctx: EventBus()),
            *integration_component_specs(),
            *context_component_specs(),
            *cognition_component_specs(),
            # --- per-turn factories (cached factory, fresh instance per turn) -
            # These resolve to a *callable* (not an instance): the factory reads
            # the ``*_kind`` schema knob at call-time and builds a fresh instance
            # each turn, so a strategy swapped mid-session is honoured next turn
            # while the graph still caches only the stateless factory (a pure
            # DAG). ``think_engine`` is stateless machinery (its per-turn result
            # now lives on RoleState), so a fresh one per run() is free.
            *watching_component_specs(
                WatchingCallbacks(
                    register_hook=self.register_hook,
                    reload_skills=self._maintenance.reload_skills_on_change,
                    reload_config=self._maintenance.reload_config_on_change,
                    reload_mcp=self._maintenance.reload_mcp_on_change,
                    reindex_code_map=self._maintenance.reindex_code_map_on_change,
                    config_source_roots=self._maintenance.config_source_roots,
                )
            ),
        ]

    # =========================================================================
    # Wiring — runtime cross-references, layered on after construction
    # =========================================================================

    def _wire_spine(self) -> None:
        """Subscribe the declarative roster onto the event bus, exactly once.

        The single place runtime cross-references between the spine and its
        subscribers are established, invoked as an explicit lifecycle step from
        ``Role._ensure_ready`` (never as a hidden side-effect of a component
        read). Split from *construction* on purpose: the ``event_bus`` component
        is a bare leaf, so the build graph is a pure DAG; the cyclic wiring
        (bus ⇄ subscribers) is layered on afterward, here, once everyone is born.
        In ``Role.run`` the bus is bound (``set_bus``) and then wired before the
        first ``emit``, so no event is ever raised onto an unwired spine.

        Idempotent: :attr:`_spine_wired` is set ``True`` *before* the body runs,
        so a redundant call (or a transitive re-entry while building a subscriber)
        short-circuits — the roster is subscribed exactly once.
        """
        if self._spine_wired:
            return
        self._spine_wired = True
        bus = self.event_bus
        for sub in self._build_event_subscribers():
            bus.subscribe(sub)

    def _wire_collaborators(self) -> None:
        """Establish runtime edges *between built collaborators*, exactly once.

        The sibling of :meth:`_wire_spine` for cross-references that are neither
        the event spine nor construction inputs: edges one component holds onto
        *another* at runtime. Kept out of the builders so constructing a component
        never mutates a sibling as a hidden side-effect — every builder stays a
        pure function of its inputs and the build graph a pure DAG.

        Today this is a single edge: the router's COMPRESS-recovery reducer is the
        ContextManager's HARD fold+drop reducer, so every LLM the router
        builds/routes (incl. the main think path via ``route(llm_config=)``) can
        shrink+re-issue an overflowing wire payload. A one-way edge
        (router ← manager) onto two leaves, so order is immaterial; it just needs
        both to exist, which this lifecycle step guarantees.

        Idempotent via :attr:`_collaborators_wired` (set before the body).
        """
        if self._collaborators_wired:
            return
        self._collaborators_wired = True
        self.router.context_reducer = self.context_manager.recovery_reducer

    def _build_event_subscribers(self) -> list:
        """The single declarative roster of every event-bus subscriber.

        One list, read top-to-bottom for humans; subscribe order is immaterial
        (the bus re-sorts each plane by stage/priority). Opt-in subscribers are
        ``None`` when their layer is off and dropped at the end, mirroring the
        turn-context roster. To add a subscriber — infra or feed — add one entry
        here; there are no hand-written ``bus.subscribe`` calls and no back-ref
        special cases (a producer like the LSP service declares ``on_subscribed``
        and the bus hands it its own handle).

        The roster spans the control-plane :class:`HookSubscriber` (when a hook
        layer exists), the always-on infra observers (:class:`RecorderSubscriber`,
        :class:`ResourceReconcileSubscriber`, :class:`LogSubscriber`) plus the
        conditional :class:`TracingSubscriber` /
        :class:`ReporterSubscriber`, the opt-in :class:`LspService` (observer +
        producer), and every dual-role turn-context feed (those exposing
        ``handle`` — an :class:`ObservationSubscriber`) pulled from the single
        :attr:`turn_context_sources` roster so the input edge can never drift out
        of sync with what renders.
        """
        subs = [
            *integration_event_subscribers(self._graph.get),
            *session_event_subscribers(self._graph.get),
            ResourceReconcileSubscriber(self.session_manager),
            LogSubscriber(),
            TracingSubscriber(LangfuseBackend(), trace_steps=step_tracing_enabled()) if is_enabled() else None,
            ReporterSubscriber(MOTE_REPORTER_DEFAULT_URL) if MOTE_REPORTER_DEFAULT_URL else None,
        ]
        subs += [s for s in self.turn_context_sources if isinstance(s, ObservationSubscriber)]
        return [s for s in subs if s is not None]

    # =========================================================================
    # Peek accessors — return the raw slot without triggering a build. Used by
    # teardown / turn-boundary paths that must not lazily construct a component.
    # =========================================================================

    def peek_resource_guard(self):
        """The live resource-cap guard if the sandbox runtime is built, else ``None``.

        Exposes the mutable :class:`ResourceGuard` so an interactive session can
        adjust caps (``set_memory_max`` etc.); the change is read fresh by the
        runtime on the next wrapped command.
        """
        return self._state.resource_guard

    def set_task_completion_wake(self, wake: "Optional[Callable]") -> None:
        """Wire a wake callback onto the background task pool.

        Called by the scheduler/REPL after adopting the role so that background
        task completions trigger a new turn instead of waiting for user input.
        The pool is built lazily and may not exist yet, so the callback is also
        stashed in ``state.pending_task_completion_wake`` for the builder to pass
        on creation, and rebinds a live pool.
        """
        self._state.pending_task_completion_wake = wake
        pool = self._graph.peek("bg_pool")
        if pool is not None:
            pool.set_wake(wake)

    # =========================================================================
    # Hook registration
    # =========================================================================

    def register_hook(self, event: str, fn, matcher: Optional[str] = None) -> None:
        """Register an in-process Python hook callback (the SDK-style path).

        Engages the hook layer even with no ``HookConfig`` declared. Register
        before ``run()`` so the executor / context manager pick up the manager.
        """
        manager = self._graph.peek("hook_manager")
        if manager is not None:
            manager.register(event, fn, matcher)
        else:
            self._state.hook_callbacks.append((event, fn, matcher))

    async def kickoff_repo_scan(self) -> None:
        await self._maintenance.kickoff_repo_scan()

    async def kickoff_workspace_cleanup(self) -> None:
        await self._maintenance.kickoff_workspace_cleanup()
