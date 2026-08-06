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
from contextlib import asynccontextmanager
from contextvars import Token
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Generic, Optional, TypeVar, cast
from uuid import uuid4

from mote.contracts.ports.artifact.store import ArtifactPublicationOutbox
from mote.contracts.ports.events.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.contracts.runtime.application import ApplicationLeasePort, RuntimeCompositionLeasePort
from mote.kernel.execution import ExecutionEngine
from mote.runtime.agent.capabilities import RoleCapabilities
from mote.runtime.agent.component_accessors import RoleComponentAccessors
from mote.runtime.agent.component_graph import BuildContext, ComponentGraph, ComponentKey, ComponentSpec
from mote.runtime.agent.component_keys import (
    ARTIFACT_REPOSITORY_BUNDLE,
    BACKGROUND_POOL,
    CAPABILITIES,
    CHECKPOINT_PAYLOAD_STORE,
    CHECKPOINT_SUBSCRIBER,
    EXECUTOR,
    HOOK_MANAGER,
    INFERENCE_PORT,
    LSP_SERVICE,
    REPO_INDEX,
    RUNTIME_CHECKPOINT_RECORDER,
    RUNTIME_HANDOFF_JOURNAL,
    RUNTIME_HOST,
    RUNTIME_OPERATION_JOURNAL,
    RUNTIME_PROJECTION_JOURNAL,
    RUNTIME_PROJECTION_RECONCILER,
    SESSION_MANAGER,
    SKILL_MANAGER,
    STATE_CTL,
    TELEMETRY,
    TITLE_SUBSCRIBER,
    WORKSPACE_STORE,
)
from mote.runtime.agent.components import (
    ContextComponentInputs,
    IntegrationComponentInputs,
    PolicyComponentInputs,
    SessionComponentInputs,
    WatchingCallbacks,
    action_component_specs,
    cognition_component_specs,
    context_component_specs,
    event_fabric_component_spec,
    integration_component_specs,
    integration_event_subscribers,
    policy_component_specs,
    session_component_specs,
    session_event_subscribers,
    watching_component_specs,
)
from mote.runtime.agent.components.action import ActionComponentInputs
from mote.runtime.agent.role_state import RoleStateController
from mote.runtime.agent.session_manager import RoleSessionManager
from mote.runtime.code_map.lifecycle import CodeMapLifecycle
from mote.runtime.code_map.scan_gate import CodeMapScanGate
from mote.runtime.events.log_subscriber import LogSubscriber
from mote.runtime.events.telemetry import TelemetryManifest, TelemetryRuntime
from mote.runtime.hook.manager import AsyncHookCallback
from mote.runtime.interactive.host import RuntimeHost
from mote.runtime.models.composition_context import (
    RuntimeCompositionLeaseView,
    bind_runtime_composition,
    reset_runtime_composition,
)
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay
from mote.runtime.session.run_lease import RunLeaseHandle, RunLeaseStore
from mote.runtime.telemetry.logging import logger
from mote.runtime.telemetry.reporting import MOTE_REPORTER_DEFAULT_URL, ReporterSubscriber

if TYPE_CHECKING:
    from mote.runtime.agent.role import Role
    from mote.runtime.tools.permission.sandbox.resource_guard import ResourceGuard


def _telemetry_spec(handler: object) -> TelemetrySubscriptionSpec:
    qualified = f"{type(handler).__module__}.{type(handler).__qualname__}".lower()
    segments = re.findall(r"[a-z0-9]+", qualified)
    identity = TelemetryIdentity("mote.telemetry." + ".".join(segments))
    return TelemetrySubscriptionSpec(
        identity=identity,
        capacity=1024,
        overflow=TelemetryOverflow.DROP_OLDEST,
    )


def _build_telemetry() -> TelemetryRuntime:
    """Build the canonical per-Role telemetry runtime without activation."""

    return TelemetryRuntime(TelemetryManifest(()))


def _build_state_controller(ctx: "BuildContext[Role, ComponentsState]") -> RoleStateController:
    return RoleStateController(ctx.role.state)


class ComponentsState:
    """Mutable per-Role extras that are not themselves graph components.

    Threaded into every builder via ``ctx.state`` so a spec can read/adjust them
    without reaching for a sibling: queued Python hook callbacks, the pending
    task-completion wake, the live resource-cap guard, and one-shot startup
    reconciliation gates.
    """

    def __init__(self) -> None:
        self.pending_task_completion_wake: "Optional[Callable]" = None
        self.hook_callbacks: list[tuple[str, AsyncHookCallback, Optional[str]]] = []
        self.resource_guard: Optional[ResourceGuard] = None
        self.output_lease: RunLeaseHandle | None = None
        self.graph_leases: dict[str, RunLeaseHandle] = {}
        self.worker_id = uuid4().hex
        self.artifact_publications_reconciled = False
        self.runtime_projections_reconciled = False
        self.artifact_reconciliation_lock = asyncio.Lock()
        self.runtime_projection_reconciliation_lock = asyncio.Lock()
        self.artifact_reconciliation_task: asyncio.Task[None] | None = None
        self.runtime_projection_reconciliation_task: asyncio.Task[None] | None = None
        self.artifact_gc_task: asyncio.Task[int] | None = None
        self.application_lease: ApplicationLeasePort | None = None
        self.runtime_composition_lease: RuntimeCompositionLeasePort | None = None
        self.runtime_composition_token: Token[RuntimeCompositionLeaseView | None] | None = None


OutputT = TypeVar("OutputT")


class RoleComponents(RoleComponentAccessors[OutputT], Generic[OutputT]):
    """Owns and lazily builds the Role's collaborators (see module docstring)."""

    def __init__(self, role: "Role"):
        self._role = role
        self._execution_engine_factory_key: ComponentKey[Callable[[], ExecutionEngine[OutputT]]] = ComponentKey(
            "execution_engine_factory"
        )
        self._state = ComponentsState()
        self._graph = ComponentGraph(role, self._state, self._component_specs())
        services = role._wiring.services
        self._code_map_lifecycle = CodeMapLifecycle(
            indexer=lambda: self._graph.get(REPO_INDEX),
            repository_root=lambda: Path(role.state.project_root or role.get_cwd()),
            session_identity=role.state.session_id,
            gate=(services.code_map_scan_gate if services is not None else None) or CodeMapScanGate(),
        )
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

    async def begin_application_lease(self) -> None:
        services = self._role._wiring.services
        composition = services.application_composition if services is not None else None
        if composition is None:
            return
        if self._state.application_lease is not None:
            raise RuntimeError("an application lease is already active")
        application_lease = await composition.acquire()
        try:
            runtime_lease = await application_lease.acquire_runtime()
        except BaseException:
            await application_lease.aclose()
            raise
        self._state.application_lease = application_lease
        self._state.runtime_composition_lease = runtime_lease
        self._state.runtime_composition_token = bind_runtime_composition(runtime_lease)

    async def end_application_lease(self) -> None:
        runtime_lease = self._state.runtime_composition_lease
        application_lease = self._state.application_lease
        token = self._state.runtime_composition_token
        self._state.runtime_composition_lease = None
        self._state.application_lease = None
        self._state.runtime_composition_token = None
        if token is not None:
            reset_runtime_composition(token)
        if runtime_lease is not None:
            await runtime_lease.aclose()
        if application_lease is not None:
            await application_lease.aclose()

    def current_runtime_composition(self):
        lease = self._state.runtime_composition_lease
        if lease is None:
            raise RuntimeError("no Runtime composition lease is active")
        return lease

    def current_application_generation_id(self) -> str:
        lease = self._state.application_lease
        if lease is None:
            raise RuntimeError("no application lease is active")
        return lease.application_generation_id.value

    async def acquire_runtime_composition(self):
        lease = self._state.application_lease
        if lease is None:
            raise RuntimeError("no application lease is active")
        return await lease.acquire_runtime()

    def current_runtime_role_config(self):
        lease = self._state.application_lease
        if lease is None:
            raise RuntimeError("no application lease is active")
        return lease.runtime_role_config

    @asynccontextmanager
    async def application_scope(self):
        await self.begin_application_lease()
        try:
            yield
        finally:
            await self.end_application_lease()

    @property
    def telemetry_wired(self) -> bool:
        return self._telemetry_wired

    async def begin_output_lease(self) -> RunLeaseHandle:
        """Acquire one ownership epoch before constructing the run loop."""
        if self._state.output_lease is not None:
            raise RuntimeError("an output lease is already active")
        restored = self._role._state_ctl.get_pending_output_restore()
        projection = self.session_projection.snapshot()
        pending_runs = tuple(projection.active_pending_act_by_run)
        if len(pending_runs) > 1:
            raise RuntimeError("Session has multiple active PendingAct runs")
        restored_run_id = (restored or {}).get("run_id")
        if restored_run_id is not None and pending_runs and restored_run_id != pending_runs[0]:
            raise RuntimeError("output restore and PendingAct restore run identities differ")
        run_id = str(restored_run_id or (pending_runs[0] if pending_runs else uuid4().hex))
        path = (
            SessionLog(
                self._role.session_id,
                base_dir=str(self.workspace_store.sessions_root),
                writer=self._role._context.disk_writer,
            ).path.parent
            / "run_leases.json"
        )
        services = self._role._wiring.services
        coordinator = (services.run_lease_coordinator if services is not None else None) or RunLeaseStore(path)
        handle = RunLeaseHandle(
            coordinator,
            run_id=run_id,
            owner_id=self._state.worker_id,
            policy=self._role._wiring.dependencies.run_lease_policy,
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
                base_dir=str(self.workspace_store.sessions_root),
                writer=self._role._context.disk_writer,
            ).path.parent
            / "run_leases.json"
        )
        services = self._role._wiring.services
        coordinator = (services.run_lease_coordinator if services is not None else None) or RunLeaseStore(path)
        handle = RunLeaseHandle(
            coordinator,
            run_id=run_id,
            owner_id=self._state.worker_id,
            policy=self._role._wiring.dependencies.run_lease_policy,
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
            task = self._state.artifact_reconciliation_task
            if task is None or task.done():
                self._state.artifact_reconciliation_task = asyncio.create_task(
                    self._run_artifact_publication_reconciliation(),
                    name="mote-artifact-publication-reconciliation",
                )

    async def _run_artifact_publication_reconciliation(self) -> None:
        delay = 0.05
        while True:
            await asyncio.sleep(delay)
            if await self._reconcile_artifact_publications():
                return
            delay = min(delay * 2, 5.0)

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
            task = self._state.runtime_projection_reconciliation_task
            if task is None or task.done():
                self._state.runtime_projection_reconciliation_task = asyncio.create_task(
                    self._run_runtime_projection_reconciliation(),
                    name="mote-runtime-projection-reconciliation",
                )

    async def _run_runtime_projection_reconciliation(self) -> None:
        delay = 0.05
        while True:
            await asyncio.sleep(delay)
            if await self._reconcile_runtime_projections():
                return
            delay = min(delay * 2, 5.0)

    async def _reconcile_runtime_projections(self) -> bool:
        async with self._state.runtime_projection_reconciliation_lock:
            if self._state.runtime_projections_reconciled:
                return True
            try:
                pending = replay(self.session_log).pending_runtime_projections
                if pending:
                    reconciler = self._graph.get(RUNTIME_PROJECTION_RECONCILER)
                    result = await reconciler.reconcile(pending.values())
                    if result.failed:
                        return False
                complete = not replay(self.session_log).pending_runtime_projections
            except Exception:
                return False
            self._state.runtime_projections_reconciled = complete
            return complete

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
        projection = self._role._wiring.dependencies.component_projection
        if projection is None:
            raise RuntimeError("Agent composition requires a Product component projection")
        return [
            # --- leaves (read no sibling) -----------------------------------
            # Behaviour holders over the Role: the state controller reads only the
            # (pure-DTO) RoleState, the capabilities holder only the Role itself.
            # Registered as leaves so the Role's ``_state_ctl`` / ``_capabilities``
            # delegators resolve them through the one graph like every other
            # collaborator (Role.__init__ builds nothing but this holder).
            ComponentSpec(STATE_CTL, _build_state_controller),
            ComponentSpec(CAPABILITIES, lambda ctx: RoleCapabilities(ctx.role)),
            ComponentSpec(SESSION_MANAGER, lambda ctx: RoleSessionManager(ctx.role)),
            ComponentSpec(
                RUNTIME_HOST,
                lambda ctx: RuntimeHost(
                    checkpoint_sink=ctx.dep(RUNTIME_CHECKPOINT_RECORDER),
                    projection_journal=ctx.dep(RUNTIME_PROJECTION_JOURNAL),
                    operation_journal=ctx.dep(RUNTIME_OPERATION_JOURNAL),
                    handoff_journal=ctx.dep(RUNTIME_HANDOFF_JOURNAL),
                    checkpoint_payload_store=ctx.dep(CHECKPOINT_PAYLOAD_STORE),
                    durability_observer=ctx.dep(TELEMETRY).emit_sync,
                ),
            ),
            *action_component_specs(inputs=projection.action()),
            *session_component_specs(projection.session()),
            ComponentSpec(
                TELEMETRY,
                lambda ctx: _build_telemetry(),
            ),
            *integration_component_specs(projection.integrations()),
            event_fabric_component_spec(),
            *policy_component_specs(projection.policy()),
            *context_component_specs(projection.context()),
            *cognition_component_specs(
                self._execution_engine_factory_key,
                inputs=projection.cognition(),
            ),
            # --- per-turn factories (cached factory, fresh instance per turn) -
            # These resolve to a *callable* (not an instance): the factory reads
            # the ``*_kind`` schema knob at call-time and builds a fresh instance
            # each turn, so a strategy swapped mid-session is honoured next turn
            # while the graph still caches only the stateless factory (a pure
            # DAG). ``inference_engine`` is stateless machinery (its per-turn result
            # now lives on RoleState), so a fresh one per run() is free.
            *watching_component_specs(
                WatchingCallbacks(
                    register_hook=self.register_hook,
                    reload_skills=self._reload_skills_on_change,
                    reload_config=self._reload_config_on_change,
                    reload_mcp=self._reload_mcp_on_change,
                    reindex_code_map=self._reindex_code_map_on_change,
                    config_source_roots=self._config_source_roots,
                ),
                projection.watching(),
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
            await telemetry.subscribe_all(_telemetry_spec(subscriber), subscriber)
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
            *integration_event_subscribers(
                lambda: self._graph.get(HOOK_MANAGER),
            ),
            *session_event_subscribers(
                lambda: self._graph.get(CHECKPOINT_SUBSCRIBER),
                lambda: self._graph.get(TITLE_SUBSCRIBER),
            ),
            LogSubscriber(),
            self._role._context.langfuse.subscriber(),
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
        pool = self._graph.peek(BACKGROUND_POOL)
        if pool is not None:
            pool.set_wake(wake)

    # =========================================================================
    # Hook registration
    # =========================================================================

    def register_hook(
        self,
        event: str,
        fn: AsyncHookCallback,
        matcher: Optional[str] = None,
    ) -> None:
        """Register an in-process Python hook callback (the SDK-style path).

        Engages the hook layer even with no ``HookConfig`` declared. Register
        before ``run()`` so the executor / context manager pick up the manager.
        """
        manager = self._graph.peek(HOOK_MANAGER)
        if manager is not None:
            manager.register_async(event, fn, matcher)
        else:
            self._state.hook_callbacks.append((event, fn, matcher))

    async def _reindex_code_map_on_change(self, hook_input: object) -> None:
        payload = getattr(hook_input, "payload", None)
        path = getattr(payload, "path", None)
        if type(path) is str and path:
            await self._code_map_lifecycle.refresh_changed_path(path)

    def _config_source_roots(self) -> list[str]:
        projection = self._role._wiring.dependencies.component_projection
        if projection is None:
            raise RuntimeError("Agent composition requires a Product component projection")
        return projection.watched_config_paths()

    async def _reload_skills_on_change(self, hook_input: object) -> None:
        del hook_input
        manager = self._graph.peek(SKILL_MANAGER)
        if manager is not None and manager.reload():
            logger.debug("skills activation source changed")

    async def _reload_config_on_change(self, hook_input: object) -> None:
        del hook_input
        services = self._role._wiring.services
        reloader = services.application_reloader if services is not None else None
        if reloader is not None:
            await reloader.reload()

    async def _reload_mcp_on_change(self, hook_input: object) -> None:
        del hook_input
        executor = self._graph.peek(EXECUTOR)
        if executor is None:
            return
        enabled = self._role.config.mcp.enabled
        await executor.reload_mcp(self._role.role_schema.mcps, enabled=enabled)

    async def kickoff_repo_scan(self) -> None:
        self._code_map_lifecycle.start_scan()

    def kickoff_artifact_gc(self) -> None:
        task = self._state.artifact_gc_task
        if task is None or task.done():
            collector = self._graph.get(ARTIFACT_REPOSITORY_BUNDLE).collector
            self._state.artifact_gc_task = asyncio.create_task(
                run_disk_io(collector.collect),
                name="mote-artifact-gc",
            )

    async def close_owner_tasks(self) -> None:
        await self._code_map_lifecycle.close()
        tasks = tuple(
            task
            for task in (
                self._state.artifact_reconciliation_task,
                self._state.runtime_projection_reconciliation_task,
                self._state.artifact_gc_task,
            )
            if task is not None and not task.done()
        )
        self._state.artifact_reconciliation_task = None
        self._state.runtime_projection_reconciliation_task = None
        self._state.artifact_gc_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(
                *(cast(Awaitable[object], task) for task in tasks),
                return_exceptions=True,
            )

    def peek_inference_port(self):
        return self._graph.peek(INFERENCE_PORT)
