#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleComponents — declarative assembly + ownership of a Role's subsystems.

The Role is pure orchestration; the wiring of its (mostly opt-in, lazily built)
collaborators — the LLM router, tool executor, context manager, Telemetry,
session log, hook/LSP/file-watch services, the per-turn context bus, etc. —
lives here. The Role keeps the public property surface (``role.executor``,
``role.telemetry`` …) as thin delegators onto this object, so external callers
and tests are unchanged.

Each collaborator is declared once as a :class:`ComponentSpec` in
:meth:`RoleComponents._component_specs` and resolved by a single lazy,
cycle-checked :class:`ComponentGraph`. A builder reads its siblings only through
``ctx.dep`` (eager, cycle-tracked) / ``ctx.defer`` (a lazy thunk), and role facts
through ``ctx.role`` / ``ctx.state`` — so *construction* is a pure DAG (a build
cycle raises :class:`ComponentCycleError` at the traversal, never a stack
overflow) and no builder mutates a sibling. The two genuinely cyclic runtime
cross-references (Telemetry handler registration and the router ← context
manager reducer edge) are layered on *afterward* by explicit lifecycle steps
(:meth:`_wire_telemetry` / :meth:`_wire_collaborators`), driven from
``Role._ensure_ready`` before the first turn.

Mutable extras that are not themselves components (queued Python hook callbacks,
the pending task-completion wake, the live resource-cap guard, one-shot startup
gates) live on
:class:`ComponentsState`, threaded to builders via ``ctx.state``. ``peek_*``
accessors expose a built slot without triggering a build (teardown paths).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Callable, Optional, cast
from uuid import uuid4

from mote.contracts.artifacts import ArtifactRetention
from mote.contracts.ports import ArtifactPublicationOutbox
from mote.contracts.ports.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.runtime.agent.capabilities import RoleCapabilities
from mote.runtime.agent.component_accessors import RoleComponentAccessors
from mote.runtime.agent.component_graph import ComponentGraph, ComponentSpec
from mote.runtime.agent.role_state import RoleStateController
from mote.runtime.agent.runtime_maintenance import RuntimeMaintenance
from mote.runtime.agent.runtime_modules import (
    WatchingCallbacks,
    action_component_specs,
    cognition_component_specs,
    context_component_specs,
    integration_component_specs,
    integration_event_subscribers,
    policy_component_specs,
    session_component_specs,
    session_event_subscribers,
    watching_component_specs,
)
from mote.runtime.agent.session_manager import RoleSessionManager
from mote.runtime.disk.async_io import run_disk_io
from mote.runtime.events import LogSubscriber
from mote.runtime.events.telemetry import TelemetryBinding, TelemetryManifest, TelemetryRuntime
from mote.runtime.interactive import RuntimeHost
from mote.runtime.reporting import MOTE_REPORTER_DEFAULT_URL, ReporterSubscriber
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay
from mote.runtime.session.run_lease import RunLeaseHandle, RunLeaseStore

if TYPE_CHECKING:
    from mote.runtime.agent.role import Role
    from mote.runtime.tools.permission.sandbox.resource_guard import ResourceGuard


def _telemetry_binding(handler: object) -> TelemetryBinding:
    qualified = f"{type(handler).__module__}.{type(handler).__qualname__}".lower()
    segments = re.findall(r"[a-z0-9]+", qualified)
    identity = TelemetryIdentity("mote.telemetry." + ".".join(segments))
    return TelemetryBinding(
        spec=TelemetrySubscriptionSpec(
            identity=identity,
            capacity=1024,
            overflow=TelemetryOverflow.DROP_OLDEST,
        ),
        handler=cast(Any, handler),
    )


class ComponentsState:
    """Mutable per-Role extras that are not themselves graph components.

    Threaded into every builder via ``ctx.state`` so a spec can read/adjust them
    without reaching for a sibling: queued Python hook callbacks, the pending
    task-completion wake, the live resource-cap guard, and one-shot startup
    reconciliation gates.
    """

    def __init__(self) -> None:
        self.pending_task_completion_wake: "Optional[Callable]" = None
        self.hook_callbacks: list[tuple[str, Any, Optional[str]]] = []
        self.resource_guard: Optional[ResourceGuard] = None
        self.output_lease: RunLeaseHandle | None = None
        self.graph_leases: dict[str, RunLeaseHandle] = {}
        self.worker_id = uuid4().hex
        self.artifact_publications_reconciled = False
        self.runtime_projections_reconciled = False
        self.artifact_reconciliation_lock = asyncio.Lock()
        self.runtime_projection_reconciliation_lock = asyncio.Lock()


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
        # Telemetry is wired exactly once, as an explicit lifecycle step
        # (``_wire_telemetry`` from ``Role._ensure_ready``), never as a side-effect of
        # constructing the leaf ``telemetry``. Set True *before* the wiring body
        # runs so a redundant/re-entrant call short-circuits (idempotent).
        self._telemetry_wired = False
        # Runtime cross-references between already-built collaborators (e.g. the
        # router ← ContextManager reducer edge), established once as an explicit
        # lifecycle step (``_wire_collaborators`` from ``Role._ensure_ready``) so
        # no getter mutates a *sibling* component as a hidden read side-effect.
        self._collaborators_wired = False
        self._event_fabric_started = False

    async def start_event_fabric(self) -> None:
        if self._event_fabric_started:
            return
        session_log = self.session_log
        session_log.exists()
        self.session_projection.restore(session_log.iter_events())
        await self.event_fabric.start()
        session_log.bind_async_sink(self.session_fact_committer.commit_event)
        self._event_fabric_started = True

    @property
    def telemetry_wired(self) -> bool:
        return self._telemetry_wired

    async def begin_output_lease(self) -> RunLeaseHandle:
        """Acquire one ownership epoch before constructing the run loop."""
        if self._state.output_lease is not None:
            raise RuntimeError("an output lease is already active")
        restored = self._role._state_ctl.get_pending_output_restore()
        run_id = str((restored or {}).get("run_id") or uuid4().hex)
        path = (
            SessionLog(
                self._role.session_id,
                writer=self._role.context.disk_writer,
            ).path.parent
            / "run_leases.json"
        )
        services = self._role.wiring.services
        coordinator = (services.run_lease_coordinator if services is not None else None) or RunLeaseStore(path)
        handle = RunLeaseHandle(
            coordinator,
            run_id=run_id,
            owner_id=self._state.worker_id,
            policy=self._role.wiring.dependencies.run_lease_policy,
        )
        await handle.start()
        self._state.output_lease = handle
        return handle

    async def end_output_lease(self) -> None:
        handle, self._state.output_lease = self._state.output_lease, None
        if handle is not None:
            await handle.close()

    def current_output_lease(self) -> RunLeaseHandle:
        handle = self._state.output_lease
        if handle is None:
            raise RuntimeError("no output lease is active")
        return handle

    async def begin_graph_lease(self, run_id: str) -> RunLeaseHandle:
        if run_id in self._state.graph_leases:
            raise RuntimeError(f"graph lease is already active: {run_id}")
        path = (
            SessionLog(
                self._role.session_id,
                writer=self._role.context.disk_writer,
            ).path.parent
            / "run_leases.json"
        )
        services = self._role.wiring.services
        coordinator = (services.run_lease_coordinator if services is not None else None) or RunLeaseStore(path)
        handle = RunLeaseHandle(
            coordinator,
            run_id=run_id,
            owner_id=self._state.worker_id,
            policy=self._role.wiring.dependencies.run_lease_policy,
        )
        await handle.start()
        self._state.graph_leases[run_id] = handle
        return handle

    async def end_graph_lease(self, run_id: str) -> None:
        handle = self._state.graph_leases.pop(run_id, None)
        if handle is not None:
            await handle.close()

    def current_graph_lease(self, run_id: str) -> RunLeaseHandle:
        try:
            return self._state.graph_leases[run_id]
        except KeyError:
            raise RuntimeError(f"no graph lease is active: {run_id}") from None

    async def reconcile_artifact_publications_once(self) -> None:
        if self._state.artifact_publications_reconciled:
            return
        complete = await self._reconcile_artifact_publications()
        if not complete:
            self._maintenance.schedule_reconciliation(
                "artifact-publication",
                self._reconcile_artifact_publications,
            )

    async def _reconcile_artifact_publications(self) -> bool:
        async with self._state.artifact_reconciliation_lock:
            if self._state.artifact_publications_reconciled:
                return True
            try:
                result = await self.artifact_publisher.reconcile_pending()
                pending = await cast(ArtifactPublicationOutbox, self.artifact_store).pending_ids(1)
            except Exception:
                return False
            complete = not (result is not None and result.failed) and not pending
            self._state.artifact_publications_reconciled = complete
            return complete

    async def reconcile_runtime_projections_once(self) -> None:
        if self._state.runtime_projections_reconciled:
            return
        complete = await self._reconcile_runtime_projections()
        if not complete:
            self._maintenance.schedule_reconciliation(
                "runtime-projection",
                self._reconcile_runtime_projections,
            )

    async def _reconcile_runtime_projections(self) -> bool:
        async with self._state.runtime_projection_reconciliation_lock:
            if self._state.runtime_projections_reconciled:
                return True
            try:
                pending = replay(self.session_log).pending_runtime_projections
                if pending:
                    reconciler = self._graph.get("runtime_projection_reconciler")
                    result = await reconciler.reconcile(pending.values())
                    if result.failed:
                        return False
                complete = not replay(self.session_log).pending_runtime_projections
            except Exception:
                return False
            self._state.runtime_projections_reconciled = complete
            return complete

    async def release_ephemeral_artifacts(self) -> None:
        """Close the turn-scoped Artifact lifetime and reclaim its CAS."""
        store = self.artifact_store
        released = await run_disk_io(
            store.release_retentions,
            (ArtifactRetention.EPHEMERAL,),
        )
        if not released:
            return
        await run_disk_io(self._graph.get("artifact_repository_bundle").collector.collect)

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
            ComponentSpec(
                "runtime_host",
                lambda ctx: RuntimeHost(
                    checkpoint_sink=ctx.dep("runtime_checkpoint_recorder"),
                    projection_journal=ctx.dep("runtime_projection_journal"),
                    operation_journal=ctx.dep("runtime_operation_journal"),
                    handoff_journal=ctx.dep("runtime_handoff_journal"),
                    checkpoint_payload_store=ctx.dep("checkpoint_payload_store"),
                    durability_observer=ctx.dep("telemetry").emit_sync,
                ),
            ),
            *action_component_specs(self._role.wiring.dependencies.background_task_pool_builder),
            *session_component_specs(),
            ComponentSpec(
                "telemetry",
                lambda ctx: TelemetryRuntime(TelemetryManifest(())),
            ),
            *integration_component_specs(),
            *policy_component_specs(),
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

    async def _wire_telemetry(self) -> None:
        """Start and bind the complete bounded telemetry roster exactly once.

        The single place telemetry handlers are registered, invoked as an
        explicit lifecycle step from
        ``Role._ensure_ready`` (never as a hidden side-effect of a component
        read). Split from *construction* on purpose: the ``telemetry`` component
        is a bare leaf, so the build graph is a pure DAG; handler registration is
        layered on afterward, here, once everyone is born.
        In ``Role.run`` telemetry is bound and then wired before the
        first ``emit``, so no event is raised before handlers are registered.

        Idempotent: :attr:`_telemetry_wired` is set ``True`` after registration,
        so a redundant call (or a transitive re-entry while building a subscriber)
        short-circuits — the roster is subscribed exactly once.
        """
        if self._telemetry_wired:
            return
        telemetry = self.telemetry
        telemetry.start()
        for subscriber in self._build_telemetry_subscribers():
            bind = getattr(subscriber, "bind_telemetry", None)
            if bind is not None:
                bind(telemetry)
            await telemetry.subscribe(_telemetry_binding(subscriber))
        self.context_manager.bind_telemetry(telemetry)
        self._telemetry_wired = True

    def _wire_collaborators(self) -> None:
        """Establish runtime edges *between built collaborators*, exactly once.

        The sibling of :meth:`_wire_telemetry` for cross-references that are not
        construction inputs: edges one component holds onto
        *another* at runtime. Kept out of the builders so constructing a component
        never mutates a sibling as a hidden side-effect — every builder stays a
        pure function of its inputs and the build graph a pure DAG.

        The router's COMPRESS-recovery reducer is the
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

    def _build_telemetry_subscribers(self) -> list:
        """The single declarative roster of telemetry subscribers.

        One list, read top-to-bottom for humans; subscribe order is immaterial
        (each handler owns an independent bounded mailbox). Opt-in handlers are
        ``None`` when their layer is off and dropped at the end, mirroring the
        turn-context roster. To add a subscriber — infra or feed — add one entry
        here; there are no scattered ``telemetry.subscribe`` calls or back-ref
        special cases.

        The roster spans the hook observation adapter (when enabled), the
        always-on :class:`LogSubscriber`, conditional tracing/reporting, the
        opt-in :class:`LspService`, and turn-context sources that explicitly set
        ``telemetry_observer = True``. Correctness projections such as resource
        reconciliation and model-context frontier rebuilds run directly after
        durable commit and never enter this lossy roster.
        """
        subs = [
            *integration_event_subscribers(self._graph.get),
            *session_event_subscribers(self._graph.get),
            LogSubscriber(),
            self._role.context.langfuse.subscriber(),
            ReporterSubscriber(MOTE_REPORTER_DEFAULT_URL) if MOTE_REPORTER_DEFAULT_URL else None,
        ]
        subs += [source for source in self.turn_context_sources if getattr(source, "telemetry_observer", False)]
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

    def kickoff_artifact_gc(self) -> None:
        self._maintenance.kickoff_artifact_gc()

    async def close_maintenance(self) -> None:
        await self._maintenance.close()
