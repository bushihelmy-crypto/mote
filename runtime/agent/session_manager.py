#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RoleSessionManager — session lifecycle behaviour (resume / fork) for a Role.

Owns session lifecycle behaviour so the Role class stays focused on the run loop
and its thin capability surface. Mirrors :class:`RoleCapabilities` / :class:`RoleStateController`:
this holder owns the *behaviour* that talks to the durable ``session/`` package —
replaying a rollout back into live history, re-seeding the resource registry,
staging managed Runtime checkpoints, and branching an independent sibling role
— while the Role keeps thin delegators (``resume_session`` / ``fork_session``)
onto these methods.

Holds the Role by reference. ``resume`` mutates the Role's live history + state
(the whole point of a resume); ``fork`` reads the Role and constructs a fresh
sibling of the same class.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mote.contracts.conversation.fields import RESOURCE_ID, RESOURCE_KIND, RESOURCE_STICKY
from mote.contracts.tool import parse_toolset_manifest
from mote.runtime.agent.errors import SessionResumeIdentityError
from mote.runtime.agent.role_state import RoleState
from mote.runtime.session import list_sessions as _list_sessions
from mote.runtime.session.attribution import HunkAttribution
from mote.runtime.session.fork import fork
from mote.runtime.session.hunk_ops import HunkOps
from mote.runtime.session.log import SessionLog
from mote.runtime.session.reconcile import reconcile_tool_calls
from mote.runtime.session.replay import replay
from mote.runtime.tools.provider import toolset_manifest

if TYPE_CHECKING:
    from mote.runtime.agent.role import Role


class RoleSessionManager:
    """Behaviour behind a Role's session resume / fork lifecycle."""

    def __init__(self, role: "Role"):
        self._role = role
        self._hunk_ops: HunkOps | None = None
        self._hunk_attribution: HunkAttribution | None = None

    @staticmethod
    def list_sessions(base_dir: str | None = None, *, cwd: str | None = None) -> list:
        return _list_sessions(base_dir, cwd=cwd)

    @property
    def hunk_ops(self) -> HunkOps:
        if self._hunk_ops is None:
            file_operations = self._role._file_operations
            self._hunk_ops = HunkOps(
                file_operations.review,
                file_operations.artifacts,
                capture_snapshot=file_operations.capture,
                mutation_factory=file_operations.mutation_factory,
                commit_mutation_set=file_operations.mutations.commit,
                resource_lease=file_operations.mutations.lease,
            )
        return self._hunk_ops

    @property
    def hunk_attribution(self) -> HunkAttribution:
        if self._hunk_attribution is None:
            file_operations = self._role._file_operations
            self._hunk_attribution = HunkAttribution(file_operations.review, file_operations.artifacts)
        return self._hunk_attribution

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
        log = SessionLog(
            role.state.session_id,
            base_dir=str(role._components.workspace_store.sessions_root),
            writer=role._context.disk_writer,
        )
        if not log.exists():
            return False

        result = replay(log)  # replay scans via iter_raw, whose drain flushes queued writes first
        self.validate_identity(result.meta or {})
        messages = self._reconcile_dangling_calls(result.model_context_messages)
        # Reap think records that need no reinstatement (their assistant message
        # is already durable, or the round never completed), leaving at most the
        # single completed think the flow will reinstate on its first think node
        # — the G1 re-pay guard, adjacent to the dangling-call reconcile above.
        # Assign in place so the ContextManager (which backs onto this same list)
        # sees the rebuilt history without re-recording it.
        role.state.context.messages[:] = messages
        role.state.routing = result.routing_state
        self.reconcile_resources(messages)

        meta = result.meta or {}
        for field_name in ("working_dir", "original_working_dir", "project_root"):
            value = meta.get(field_name)
            if value:
                setattr(role.state, field_name, value)

        role.state.recovered = True
        state_ctl = role._state_ctl
        state_ctl.set_consumed_inference_checkpoints(tuple(result.consumed_inference_checkpoints.values()))
        runtime_checkpoints = getattr(result, "runtime_checkpoints", {}) or {}
        for checkpoint in runtime_checkpoints.values():
            role._components.runtime_host.stage_checkpoint(checkpoint, alias=checkpoint.alias)
        output_states = getattr(result, "output_states", {}) or {}
        agent_states = [state for state in output_states.values() if state.get("run_kind", "agent") == "agent"]
        output_state = agent_states[-1] if agent_states else result.output_state
        if output_state and output_state.get("run_kind", "agent") != "agent":
            output_state = None
        if output_state and output_state.get("status") == "committed":
            state_ctl.set_pending_output_restore(output_state)
        state_ctl.set_pending_graph_output_restores(
            {
                run_id: state
                for run_id, state in output_states.items()
                if state.get("run_kind") == "graph" and state.get("status") == "committed"
            }
        )
        return True

    @staticmethod
    def _role_identity(role: "Role") -> str:
        """The stable identity string recorded in ``session_meta.role_class``."""
        try:
            return role.residency_definition_id
        except (TypeError, ValueError) as exc:
            raise SessionResumeIdentityError("role has no stable persistence definition identity") from exc

    def validate_identity(self, meta: Mapping[str, object]) -> None:
        """Refuse to resume a session recorded by a different Role class.

        ``session_meta`` records the ``role_class`` that created the log (see
        ``Role.start_session``). Replaying that history into an incompatible Role
        would feed it a transcript it was never built for, so a genuine mismatch
        is refused fail-closed with :class:`SessionResumeIdentityError` (rather
        than silently replaying).

        Model is intentionally not part of identity because a session may resume
        under a different model.
        """
        recorded = meta["role_class"]
        current = self._role_identity(self._role)
        if recorded != current:
            raise SessionResumeIdentityError(
                f"cannot resume session recorded by role '{recorded}' into role "
                f"'{current}': resumed sessions must use the same role class."
            )

        recorded_toolsets = meta["toolset_manifest"]
        projection = self._role._wiring.dependencies.component_projection
        if projection is None:
            raise RuntimeError("Agent composition requires a Product component projection")
        expected_manifest = toolset_manifest(projection.action().toolsets)
        actual_manifest = parse_toolset_manifest(recorded_toolsets)
        if actual_manifest != expected_manifest:
            expected = [identity.to_payload() for identity in expected_manifest]
            actual = [identity.to_payload() for identity in actual_manifest]
            raise SessionResumeIdentityError(
                "cannot resume session with different Toolset dependencies: "
                f"recorded={actual!r}, current={expected!r}. Bump Toolset versions "
                "when behavior changes and resume with the original manifest."
            )

    def _reconcile_dangling_calls(self, messages):
        """Heal tool calls left dangling by a mid-turn crash, using the ledger.

        A crash between an assistant ``tool_calls`` message being flushed and its
        results being recorded leaves a dangling call that would 400 the next
        provider request. :func:`reconcile_tool_calls` splices a synthetic result
        after each such call — healing it verbatim from the ledger when the effect
        actually completed, or flagging ``<unknown-after-crash>`` when an EXTERNAL
        effect's outcome was lost. Reconciliation never deletes effect facts;
        retention remains owned by the Tool effect store lifecycle.

        A no-op when the executor has no ledger (feature disabled) — the replayed
        history is returned unchanged.
        """
        effect_store = self._role._executor.effect_store
        if effect_store is None or not self._role._executor.effect_store_config.enabled:
            return messages
        outcome = reconcile_tool_calls(messages, effect_store)
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
        self._role._components.resource_registry.reset()
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
        registry = self._role._components.resource_registry
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
        registry = self._role._components.resource_registry
        for name, desc in self._role._executor.describe_deferred_tools(revealed).items():
            if desc:
                registry.load(id=name, kind="tool", content=desc, sticky=True)

    async def fork(self) -> "Role":
        """Branch a sibling role off this session at its current history.

        Seeds a brand-new ``rollout.jsonl`` from this role's session (replayed to
        its final state) and records ``parent_session_id`` lineage on the child's
        ``session_meta``. Returns a fresh role of the same class, sharing the
        injected context/config, pinned to the new session and resumed onto the
        inherited history. The two sessions are independent afterwards: mutating
        the fork never touches this role's log.
        """
        role = self._role
        child_id = await fork(
            role.state.session_id,
            writer=role._context.disk_writer,
        )

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
            config=role._config,
            wiring=role._wiring.for_incarnation(),
        )
        forked.resume_session()
        return forked
