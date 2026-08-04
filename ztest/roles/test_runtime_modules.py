"""Ownership contracts for domain component manifests."""

import asyncio
from collections.abc import Callable

import pytest

from mote.contracts.artifact import ArtifactPublicationState, ArtifactPublishRequest, ArtifactRepresentationInput
from mote.contracts.conversation import UserMessage
from mote.contracts.file import TransactionStatus
from mote.contracts.ports.artifact.store import ArtifactResolver, ArtifactStore
from mote.contracts.ports.events.subscription import Reliability
from mote.contracts.runtime import CheckpointFidelity, RuntimeCheckpoint, RuntimeCommitFact, RuntimeProjectionIntent
from mote.contracts.surface import CanvasDocument, CanvasRectangle
from mote.kernel.execution import ExecutionEngine
from mote.runtime.agent.component_graph import ComponentKey
from mote.runtime.agent.component_keys import ARTIFACT_REPOSITORY_BUNDLE, RUNTIME_PROJECTION_JOURNAL
from mote.runtime.agent.components import (
    WatchingCallbacks,
    action_component_specs,
    cognition_component_specs,
    context_component_specs,
    integration_component_specs,
    policy_component_specs,
    session_component_specs,
    watching_component_specs,
)
from mote.runtime.artifacts import ContentAddressedArtifactBlobStore, DurableArtifactStore
from mote.runtime.fileops.edit_plans import WholeFileEditPlanRequest
from mote.runtime.fileops.identity import path_token
from mote.runtime.interactive.canvas.svg import render_canvas_svg
from mote.runtime.interactive.checkpoint_codec import encode_inline_json
from mote.runtime.projections import artifact_representation_set_digest
from mote.runtime.session import MessageEvent
from mote.runtime.session import log as session_log_module
from mote.runtime.session.projection import SESSION_PROJECTION_SUBSCRIPTION
from mote.runtime.session.replay import replay


def _watching_specs():
    async def handler(event):
        return None

    return watching_component_specs(
        WatchingCallbacks(
            register_hook=lambda event, callback, matcher: None,
            reload_skills=handler,
            reload_config=handler,
            reload_mcp=handler,
            reindex_code_map=handler,
            config_source_roots=lambda: [],
        )
    )


def _cognition_specs():
    key: ComponentKey[Callable[[], ExecutionEngine[object]]] = ComponentKey("execution_engine_factory")
    return cognition_component_specs(key)


def test_session_module_owns_complete_component_keyset():
    assert {spec.name for spec in session_component_specs()} == {
        "session_log",
        "session_projection",
        "subscription_state_store",
        "session_fact_committer",
        "file_operations",
        "artifact_repository_bundle",
        "artifact_blob_store",
        "artifact_store",
        "artifact_resolver",
        "artifact_publisher",
        "checkpoint_payload_store",
        "runtime_projection_journal",
        "runtime_operation_journal",
        "runtime_handoff_journal",
        "runtime_projection_registry",
        "runtime_projection_reconciler",
        "checkpoint_subscriber",
        "title_subscriber",
        "runtime_checkpoint_recorder",
    }


def test_session_manifest_has_no_duplicate_keys():
    names = [spec.name for spec in session_component_specs()]
    assert len(names) == len(set(names))


def test_role_manifest_owns_one_durable_session_projection(role):
    fabric = role._components.event_fabric
    workers = fabric.dispatcher.subscriptions

    assert len(workers) == 1
    assert workers[0].spec.identity == SESSION_PROJECTION_SUBSCRIPTION
    assert workers[0].spec.reliability is Reliability.DURABLE
    assert role._components.session_projection.stream_id == role.session_log.stream_id
    assert not role._components.subscription_state_store.path.exists()


@pytest.mark.asyncio
async def test_role_session_projection_tracks_commits_and_persists_barrier(role):
    await role._ensure_ready()
    try:
        await role._emit_session_start()
        message = UserMessage(content="durable projection")
        result = await role.session_log.append(MessageEvent(message))
        await role._components.event_fabric.wait_until(
            SESSION_PROJECTION_SUBSCRIPTION,
            role.session_log.stream_id,
            result.last_sequence,
        )

        state = role._components.session_projection.snapshot()
        assert state.through_sequence == result.last_sequence
        assert [item.content for item in state.transcript_messages] == ["durable projection"]
        assert (
            await role._components.subscription_state_store.load(
                SESSION_PROJECTION_SUBSCRIPTION,
                role.session_log.stream_id,
            )
            == result.last_sequence
        )
    finally:
        await role.cleanup()


@pytest.mark.asyncio
async def test_history_clear_applies_local_projections_before_return(
    role,
    tmp_path,
    monkeypatch,
):
    await role._ensure_ready()
    try:
        await role._emit_session_start()
        await role.context_manager.add(UserMessage(content="clear me"))
        before_clear = role._components.event_fabric.dispatcher.cursor(role.session_log.stream_id)
        await role._components.event_fabric.wait_until(
            SESSION_PROJECTION_SUBSCRIPTION,
            role.session_log.stream_id,
            before_clear,
        )

        role.resource_registry.load(
            id="stale-resource",
            kind="skill",
            content="stale",
            sticky=True,
        )
        tool_catalog = next(source for source in role.turn_context_bus._sources if source.name == "tool_catalog")
        tool_catalog._sent_names = {"stale-tool"}

        await role.context_manager.clear()

        committed = role._components.event_fabric.dispatcher.cursor(role.session_log.stream_id)
        assert committed > before_clear
        assert len(role.resource_registry) == 0
        assert tool_catalog._sent_names == set()
        assert tool_catalog not in role._components._build_telemetry_subscribers()

        await role._components.event_fabric.wait_until(
            SESSION_PROJECTION_SUBSCRIPTION,
            role.session_log.stream_id,
            committed,
        )
        state = role._components.session_projection.snapshot()
        assert state.through_sequence == committed
        assert state.transcript_messages == []
        assert state.model_context_messages == []
    finally:
        await role.cleanup()


@pytest.mark.asyncio
async def test_role_file_mutation_commits_through_running_fabric(
    role,
    tmp_path,
    monkeypatch,
):
    role.state.working_dir = str(tmp_path)
    role.state.project_root = str(tmp_path)
    await role._ensure_ready()
    try:
        await role._emit_session_start()
        initial_sequence = role._components.event_fabric.dispatcher.cursor(role.session_log.stream_id)
        target = tmp_path / "fabric-edit.txt"
        capabilities = role.tool_capabilities()

        plan = await capabilities["plan_file_edit"](
            WholeFileEditPlanRequest(
                path=path_token(target),
                content="committed through fabric\n",
            )
        )
        outcome = await capabilities["commit_edit_plan"](plan.plan_id)
        committed_sequence = role._components.event_fabric.dispatcher.cursor(role.session_log.stream_id)
        await role._components.event_fabric.wait_until(
            SESSION_PROJECTION_SUBSCRIPTION,
            role.session_log.stream_id,
            committed_sequence,
        )

        assert outcome.result.status is TransactionStatus.COMMITTED
        assert target.read_text() == "committed through fabric\n"
        assert committed_sequence > initial_sequence
        assert role._components.session_projection.snapshot().through_sequence == committed_sequence
    finally:
        await role.cleanup()


def test_artifact_store_wiring_uses_workspace_artifact_repository(role):
    store = role.artifact_store

    assert isinstance(store, DurableArtifactStore)
    assert isinstance(store, ArtifactStore)
    workspace_root = role.session_log.workspace_root
    assert store.index_path == workspace_root / ".artifacts" / "artifacts.sqlite3"
    assert isinstance(store._blobs, ContentAddressedArtifactBlobStore)
    bundle = role._components._graph.get(ARTIFACT_REPOSITORY_BUNDLE)
    assert store._blobs._repository is bundle.repository
    assert bundle.repository is not role.file_operations.artifacts
    assert role._components.artifact_store is store

    resolver = role.artifact_resolver
    assert isinstance(resolver, ArtifactResolver)
    assert role._components.artifact_resolver is resolver

    publisher = role.artifact_publisher
    assert role._components.artifact_publisher is publisher
    assert role.tool_capabilities()["get_artifact_publisher"]() is publisher


@pytest.mark.asyncio
async def test_role_readiness_reconciles_runtime_projection_from_checkpoint(role):
    try:
        await role._emit_session_start()
        document = CanvasDocument(
            elements=[
                CanvasRectangle(
                    id="durable-node",
                    x=5,
                    y=10,
                    width=80,
                    height=40,
                )
            ]
        )
        encoded = encode_inline_json(
            document.model_dump(mode="json"),
            codec="canvas-document+json@1",
            fidelity=CheckpointFidelity.FULL,
        )
        checkpoint = RuntimeCheckpoint(
            runtime_id="canvas-runtime",
            kind="canvas",
            epoch=1,
            revision=2,
            codec=encoded.codec,
            schema_version=encoded.schema_version,
            payload_ref=encoded.payload_ref,
            digest=encoded.digest,
            fidelity=CheckpointFidelity.FULL,
        )
        fact = RuntimeCommitFact(
            commit_id="canvas-runtime.1.2",
            checkpoint=checkpoint,
            projections=(
                RuntimeProjectionIntent(
                    intent_id="artifact",
                    projector="canvas-artifact",
                    schema_version=1,
                ),
            ),
            reason="write-commit",
        )
        journal = role._components._graph.get(RUNTIME_PROJECTION_JOURNAL)
        await journal.record_commit(fact)

        await role._ensure_ready()

        assert replay(role.session_log).pending_runtime_projections == {}
        artifact_id = "canvas-" + artifact_representation_set_digest(
            (
                ArtifactRepresentationInput(
                    representation="svg",
                    kind="canvas",
                    mime_type="image/svg+xml",
                    content=render_canvas_svg(document).encode("utf-8"),
                    suggested_name="canvas.svg",
                ),
            )
        )
        revision = await role.artifact_store.get_revision(artifact_id, 1)
        assert b'id="durable-node"' in await role.artifact_store.read(revision.get("svg"))
    finally:
        await role.cleanup()


@pytest.mark.asyncio
async def test_role_readiness_reconciles_staged_artifact_publications(
    role,
    monkeypatch,
    tmp_path,
):
    request = ArtifactPublishRequest(
        representations=(
            ArtifactRepresentationInput(
                representation="text",
                kind="report",
                mime_type="text/plain",
                content=b"durable report",
            ),
        ),
    )
    try:
        await role.artifact_store.stage("runtime:report:1", request)

        await role._ensure_ready()

        assert await role.artifact_store.pending_ids() == ()
    finally:
        await role.cleanup()


@pytest.mark.asyncio
async def test_artifact_reconcile_once_retries_transient_batch_failure(
    role,
    monkeypatch,
    tmp_path,
):
    publisher = role.artifact_publisher
    await role.artifact_store.pending_ids()
    calls = []

    async def fail_once():
        calls.append("failed")
        raise OSError("temporary index failure")

    async def succeed():
        calls.append("succeeded")

    monkeypatch.setattr(publisher, "reconcile_pending", fail_once)
    await role._components.reconcile_artifact_publications_once()
    monkeypatch.setattr(publisher, "reconcile_pending", succeed)
    await role._components.reconcile_artifact_publications_once()
    await role._components.reconcile_artifact_publications_once()

    assert calls == ["failed", "succeeded"]
    await role._components.close_owner_tasks()


@pytest.mark.asyncio
async def test_artifact_reconcile_dead_letters_permanent_item_in_current_role(
    role,
    monkeypatch,
    tmp_path,
):
    request = ArtifactPublishRequest(
        artifact_id="fixed-report",
        expected_revision=0,
        representations=(
            ArtifactRepresentationInput(
                representation="text",
                kind="report",
                mime_type="text/plain",
                content=b"conflicting report",
            ),
        ),
    )
    await role.artifact_store.publish(
        ArtifactPublishRequest(
            artifact_id="fixed-report",
            representations=(
                ArtifactRepresentationInput(
                    representation="text",
                    kind="report",
                    mime_type="text/plain",
                    content=b"existing report",
                ),
            ),
        )
    )
    await role.artifact_store.stage("runtime:report:conflict", request)

    await role._components.reconcile_artifact_publications_once()
    await role._components.reconcile_artifact_publications_once()

    failed = await role.artifact_store.load("runtime:report:conflict")
    assert failed.attempts == 1
    assert failed.state is ArtifactPublicationState.DEAD_LETTER
    await role._components.close_owner_tasks()


@pytest.mark.asyncio
async def test_artifact_reconciliation_drains_more_than_one_batch_in_current_role(
    role,
    monkeypatch,
    tmp_path,
):
    request = ArtifactPublishRequest(
        representations=(
            ArtifactRepresentationInput(
                representation="text",
                kind="report",
                mime_type="text/plain",
                content=b"backlog",
            ),
        ),
    )
    for index in range(101):
        await role.artifact_store.stage(f"backlog:{index}", request)

    await role._components.reconcile_artifact_publications_once()
    for _ in range(100):
        if not await role.artifact_store.pending_ids(1):
            break
        await asyncio.sleep(0.01)

    assert await role.artifact_store.pending_ids(1) == ()
    await role._components.close_owner_tasks()


def test_integrations_module_owns_complete_component_keyset():
    assert {spec.name for spec in integration_component_specs()} == {
        "hook_manager",
        "lsp_service",
        "diagnostics_buffer",
        "sandbox_runtime",
        "secret_store",
    }


def test_policy_module_owns_complete_component_keyset():
    assert {spec.name for spec in policy_component_specs()} == {
        "prompt_policy",
        "run_completion_policy",
    }


def test_domain_manifests_do_not_claim_the_same_component():
    names = [
        spec.name
        for spec in [
            *action_component_specs(),
            *_cognition_specs(),
            *context_component_specs(),
            *session_component_specs(),
            *integration_component_specs(),
            *policy_component_specs(),
            *_watching_specs(),
        ]
    ]
    assert len(names) == len(set(names))


def test_action_module_owns_complete_component_keyset():
    assert {spec.name for spec in action_component_specs()} == {
        "workspace_store",
        "bg_pool",
        "tool_call_policy",
        "tool_result_policy",
        "executor",
        "command_channel",
        "graph_output_service",
        "browser_profile_store",
    }


def test_context_module_owns_complete_component_keyset():
    assert {spec.name for spec in context_component_specs()} == {
        "skill_manager",
        "resource_registry",
        "context_manager",
        "context_visibility",
        "repo_index",
        "turn_context_sources",
        "turn_context_bus",
    }


def test_cognition_module_owns_complete_component_keyset():
    assert {spec.name for spec in _cognition_specs()} == {
        "router",
        "inference_port",
        "tool_snapshot_manager",
        "context_provider",
        "inference_engine_factory",
        "inference_subsystems_factory",
        "execution_engine_factory",
    }


def test_watching_module_owns_complete_component_keyset():
    assert {spec.name for spec in _watching_specs()} == {"file_watch_service"}
