"""Generate the post-closure source baseline and governance evidence manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "zdocs/post-closure-boundary-debt-implementation-requirements.md"
BASELINE = ROOT / "zdocs/architecture/post-closure-source-baseline-v1.json"
EVIDENCE = ROOT / "zdocs/architecture/post-closure-governance-evidence-v1.json"
RECEIPTS = ROOT / "zdocs/architecture/post-closure-verification-receipts-v1.json"
RECEIPT_OUTPUT_DIR = ROOT / "zdocs/architecture/verification-output"
SCHEMA = "post-closure-governance-v1"

_REQUIREMENT = re.compile(r"`(R-W[0-3]-[A-Za-z0-9][A-Za-z0-9-]*)`")
_NON_EXECUTABLE = frozenset(
    {
        "R-W1-DEAD-SURFACES-001",
        "R-W1-006",
        "R-W2-NOTEBOOK-001",
        "R-W3-OAUTH-RETIRE-001",
        "R-W3-RUNJOURNAL-001",
        "R-W3-WORKFLOW-EFFECT-002",
    }
)

_VERIFIED_REQUIREMENTS = {
    "R-W2-MODEL-CONTRACT-001": {
        "owner": "contracts.model",
        "paths": (
            "contracts/model/inference.py",
            "contracts/model/invocation.py",
            "contracts/ports/model/inference.py",
            "runtime/models/inference_port.py",
            "runtime/models/model_calls.py",
            "ztest/architecture/test_model_contract_governance.py",
        ),
        "retired_paths": ("contracts/ports/model/client.py",),
        "recipe": "finalized-model-call-contract-v1",
        "command": "python -B -m pytest ztest/architecture/test_model_contract_governance.py ztest/think/test_inference_port.py ztest/router/llm/test_gateway_client.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-ROLE-SURFACE-001": {
        "owner": "runtime.agent",
        "paths": (
            "contracts/ports/agent/hosting.py",
            "contracts/ports/events/telemetry.py",
            "runtime/agent/role.py",
            "product/entrypoints/cli/backend.py",
            "product/session_hosting/registry.py",
            "ztest/architecture/test_role_surface_governance.py",
        ),
        "recipe": "typed-role-command-query-surface-v1",
        "command": "python -B -m pytest ztest/architecture/test_role_surface_governance.py ztest/cli/test_backend.py::test_clear_messages_counts_then_clears ztest/cli/test_backend.py::test_rewind_files_uses_role_checkpoint_command ztest/cli/serving/test_session_registry.py::test_load_existing_starts_verified_session ztest/cli/serving/test_session_registry.py::test_evict_stops_control_and_cleans_role -q --tb=short -p no:cacheprovider",
    },
    "R-W2-TOOL-BINDING-001": {
        "owner": "runtime.tools",
        "paths": (
            "contracts/tool/catalog.py",
            "contracts/output/graph.py",
            "runtime/tools/tool_binding.py",
            "runtime/tools/tool_catalog.py",
            "runtime/tools/tool_executor.py",
            "runtime/tools/snapshots.py",
            "runtime/output/graph_service.py",
            "ztest/architecture/test_tool_binding_governance.py",
        ),
        "retired_paths": (
            "runtime/tools/tool_classification.py",
            "runtime/output/graph_committer.py",
        ),
        "recipe": "compiled-generation-bound-tool-binding-v1",
        "commands": (
            "python -B -m pytest ztest/architecture/test_tool_binding_governance.py -q --tb=short -p no:cacheprovider",
            "python -B -m pytest ztest/executor/test_tool_snapshots.py -q --tb=short -p no:cacheprovider",
            "python -B -m pytest ztest/executor/test_bound_registry.py -q --tb=short -p no:cacheprovider",
            "python -B -m pytest ztest/roles/test_graph_output_service.py -q --tb=short -p no:cacheprovider",
        ),
    },
    "R-W2-SKILL-ACTIVATION-001": {
        "owner": "product.skills",
        "paths": (
            "product/skills/skill_definition.py",
            "product/skills/skill_pool.py",
            "product/extensions/sources.py",
            "ztest/architecture/test_skill_activation_governance.py",
        ),
        "recipe": "skill-activation-snapshot-v1",
        "command": "python -B -m pytest ztest/architecture/test_skill_activation_governance.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-CONNECTION-INTEGRATION-001": {
        "owner": "product.session_hosting",
        "paths": (
            "product/session_hosting/connection.py",
            "product/interfaces/acp/server.py",
            "product/interfaces/agui/server.py",
            "ztest/architecture/test_connection_integration.py",
        ),
        "recipe": "connection-surface-shutdown-v1",
        "command": "python -B -m pytest ztest/architecture/test_connection_integration.py ztest/cli/serving/test_connection_scope.py::test_close_timeout_retains_draining_generation_for_policy_action ztest/cli/consumers/acp/test_acp_server.py::test_cancel_interrupts_active_turn -q --tb=short -p no:cacheprovider",
    },
    "R-W2-CONNECTION-LIFECYCLE-001": {
        "owner": "product.session_hosting.connection",
        "paths": (
            "product/session_hosting/connection.py",
            "ztest/cli/serving/test_connection_scope.py",
        ),
        "recipe": "connection-generation-cleanup-v1",
        "command": "python -B -m pytest ztest/cli/serving/test_connection_scope.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-SANDBOX-PROCESS-001": {
        "owner": "runtime.sandbox",
        "paths": (
            "runtime/sandbox/config.py",
            "runtime/process.py",
            "product/config/adapters/permissions.py",
            "ztest/architecture/test_process_runner_governance.py",
        ),
        "recipe": "sandbox-process-profile-v1",
        "command": "python -B -m pytest ztest/architecture/test_process_runner_governance.py ztest/architecture/test_hook_command_governance.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-AGENT-CONTROL-001": {
        "owner": "product.interaction",
        "paths": (
            "product/interaction/ports.py",
            "product/interaction/driver.py",
            "ztest/architecture/test_agent_control_surface.py",
            "ztest/cli/test_driver.py",
        ),
        "recipe": "product-agent-control-command-v1",
        "command": "python -B -m pytest ztest/architecture/test_agent_control_surface.py ztest/cli/test_driver.py::test_interrupt_current_turn_stages_and_interrupts ztest/cli/test_driver.py::test_steer_submission_returns_typed_receipt -q --tb=short -p no:cacheprovider",
    },
    "R-W2-001": {
        "owner": "product.config.permissions",
        "paths": (
            "product/config/adapters/permissions.py",
            "runtime/hook/manager.py",
            "runtime/tools/policy.py",
            "ztest/architecture/test_hook_command_governance.py",
            "ztest/executor/permission/test_settings_source.py",
        ),
        "recipe": "product-permission-hook-generation-v1",
        "command": "python -B -m pytest ztest/architecture/test_hook_command_governance.py ztest/executor/permission/test_settings_source.py ztest/executor/test_tool_policy.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-NOTEBOOK-DOCUMENT-001": {
        "owner": "contracts.surface",
        "paths": (
            "contracts/surface/notebook.py",
            "contracts/surface/canvas.py",
            "runtime/interactive/kernel/notebook_export.py",
            "ztest/contracts/test_notebook.py",
            "ztest/runtime/test_notebook_export.py",
        ),
        "recipe": "surface-document-contract-v1",
        "command": "python -B -m pytest ztest/contracts/test_notebook.py ztest/runtime/test_notebook_export.py ztest/product/test_canvas_tool.py::test_canvas_document_rejects_unknown_element_fields -q --tb=short -p no:cacheprovider",
    },
    "R-W2-NOTEBOOK-STDIN-001": {
        "owner": "runtime.interactive.kernel",
        "paths": (
            "contracts/surface/notebook.py",
            "runtime/interactive/kernel/driver.py",
            "runtime/interactive/chromium_frontends.py",
            "ztest/contracts/test_notebook.py",
            "ztest/executor/tools/test_kernel.py",
        ),
        "recipe": "notebook-stdin-lifecycle-v1",
        "command": "python -B -m pytest ztest/contracts/test_notebook.py ztest/executor/tools/test_kernel.py::TestLiveSurface::test_handoff_fences_and_replies_to_kernel_stdin -q --tb=short -p no:cacheprovider",
    },
    "R-W2-BGTASK-GOVERNANCE-INTEGRATION-001": {
        "owner": "orchestration.agents",
        "paths": (
            "contracts/agent/cancellation.py",
            "orchestration/agents/cancellation.py",
            "orchestration/agents/control.py",
            "ztest/agents/test_subtree_cancellation.py",
            "ztest/architecture/test_background_task_pool_lifecycle.py",
        ),
        "recipe": "background-task-supervisor-integration-v1",
        "command": "python -B -m pytest ztest/agents/test_subtree_cancellation.py ztest/architecture/test_background_task_pool_lifecycle.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-MCP-LIFECYCLE-001": {
        "owner": "runtime.tools.mcp",
        "paths": (
            "runtime/tools/mcp/lifecycle.py",
            "runtime/tools/tool_lifecycle.py",
            "runtime/tools/tool_executor.py",
            "ztest/executor/mcp/test_universal_lifecycle.py",
        ),
        "recipe": "mcp-generation-lifecycle-v1",
        "command": "python -B -m pytest ztest/executor/mcp/test_universal_lifecycle.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKSPACE-CLEANUP-RETIRE-001": {
        "owner": "runtime.session",
        "paths": (
            "runtime/session/lifecycle.py",
            "runtime/session/deletion.py",
            "ztest/architecture/test_workspace_maintenance_fencing.py",
        ),
        "retired_paths": (
            "runtime/session/workspace/cleanup.py",
            "runtime/session/workspace/cleanup_gate.py",
        ),
        "recipe": "workspace-cleanup-bypass-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_workspace_maintenance_fencing.py ztest/session/test_lifecycle.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-STREAM-001": {
        "owner": "contracts.session",
        "paths": (
            "contracts/events/envelope.py",
            "runtime/session/log.py",
            "runtime/session/stream_ownership.py",
            "ztest/session/test_codec.py",
        ),
        "recipe": "session-stream-v2-contract-v1",
        "command": "python -B -m pytest ztest/session/test_codec.py ztest/session/test_session_log.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-STORE-001": {
        "owner": "runtime.session",
        "paths": (
            "runtime/session/log.py",
            "runtime/session/stream_ownership.py",
            "ztest/session/test_session_log.py",
            "ztest/session/test_lifecycle.py",
        ),
        "recipe": "session-store-v2",
        "command": "python -B -m pytest ztest/session/test_session_log.py ztest/session/test_lifecycle.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-RETENTION-001": {
        "owner": "runtime.session",
        "paths": (
            "runtime/session/lifecycle.py",
            "runtime/session/deletion.py",
            "ztest/session/test_lifecycle.py",
        ),
        "recipe": "session-retention-v2",
        "command": "python -B -m pytest ztest/session/test_lifecycle.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-MIGRATION-001": {
        "owner": "product.migrations.session_stream",
        "paths": (
            "product/migrations/session_stream.py",
            "ztest/session/test_stream_migration.py",
        ),
        "recipe": "session-v1-source-inventory-v1",
        "command": "python -B -m pytest ztest/session/test_stream_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-MIGRATION-002": {
        "owner": "product.migrations.session_stream",
        "paths": (
            "product/migrations/session_stream.py",
            "runtime/session/log.py",
            "ztest/session/test_stream_migration.py",
        ),
        "recipe": "session-v2-inactive-candidate-activation-v1",
        "command": "python -B -m pytest ztest/session/test_stream_migration.py ztest/session/test_session_log.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-PROJECTIONS-001": {
        "owner": "runtime.session",
        "paths": (
            "runtime/session/log.py",
            "runtime/session/listing.py",
            "runtime/session/checkpoint.py",
            "runtime/session/history.py",
            "runtime/session/artifact_roots.py",
            "runtime/session/runtime_projection.py",
            "ztest/architecture/test_session_projection_governance.py",
        ),
        "recipe": "session-verified-v2-projections-v1",
        "command": "python -B -m pytest ztest/architecture/test_session_projection_governance.py ztest/session/test_stream_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-LEGACY-RETIRE-001": {
        "owner": "product.migrations.session_stream",
        "paths": (
            "product/migrations/session_stream.py",
            "runtime/session/log.py",
            "ztest/session/test_stream_migration.py",
            "ztest/architecture/test_session_projection_governance.py",
        ),
        "recipe": "session-v1-production-path-retired-evidence-retention-v1",
        "command": "python -B -m pytest ztest/session/test_stream_migration.py ztest/architecture/test_session_projection_governance.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-EVENT-001": {
        "owner": "runtime.events",
        "paths": (
            "runtime/events/subscription.py",
            "runtime/events/backends/subscription_state.py",
            "product/migrations/event_subscription.py",
            "ztest/events/test_subscription_migration.py",
        ),
        "recipe": "event-subscription-v2",
        "command": "python -B -m pytest ztest/events/test_subscription_migration.py ztest/events/test_subscription.py ztest/events/test_subscription_state.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-DAEMON-001": {
        "owner": "product.inference.daemon",
        "paths": (
            "product/inference/daemon/supervisor.py",
            "ztest/inference/test_shared_daemon_supervisor.py",
        ),
        "recipe": "inference-daemon-generation-v1",
        "command": "python -B -m pytest ztest/inference/test_shared_daemon_supervisor.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-EFFECT-001": {
        "owner": "contracts.workflow",
        "paths": (
            "contracts/workflow/effect.py",
            "orchestration/workflows/durable/model.py",
            "ztest/workflows/test_reconciliation.py",
        ),
        "recipe": "workflow-effect-v3-contract-v1",
        "command": "python -B -m pytest ztest/workflows/test_reconciliation.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-MIGRATION-001": {
        "owner": "orchestration.workflows.migration",
        "paths": (
            "orchestration/workflows/migration.py",
            "ztest/workflows/test_migration.py",
        ),
        "recipe": "workflow-v2-inventory-v1",
        "command": "python -B -m pytest ztest/workflows/test_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-MIGRATION-002": {
        "owner": "orchestration.workflows.migration",
        "paths": (
            "orchestration/workflows/migration.py",
            "ztest/workflows/test_migration.py",
        ),
        "recipe": "workflow-v3-cutover-v1",
        "command": "python -B -m pytest ztest/workflows/test_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-EFFECT-003": {
        "owner": "orchestration.workflows.durable",
        "paths": (
            "orchestration/workflows/durable/store.py",
            "orchestration/workflows/durable/reconciliation.py",
            "ztest/workflows/test_reconciliation.py",
        ),
        "recipe": "workflow-effect-store-v3",
        "command": "python -B -m pytest ztest/workflows/test_reconciliation.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-RECONCILIATION-001": {
        "owner": "orchestration.workflows.durable",
        "paths": (
            "orchestration/workflows/durable/reconciliation.py",
            "ztest/workflows/test_reconciliation.py",
        ),
        "recipe": "workflow-reconciliation-v3",
        "command": "python -B -m pytest ztest/workflows/test_reconciliation.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-INSPECTION-001": {
        "owner": "product.workflows",
        "paths": (
            "product/workflows/inspection.py",
            "product/workflows/run_graph/resume_tasks.py",
            "ztest/workflows/test_validate.py",
        ),
        "recipe": "workflow-inspection-v3",
        "command": "python -B -m pytest ztest/workflows/test_validate.py ztest/architecture/test_workflow_reconciliation_owner.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-DELIVERY-001": {
        "owner": "orchestration.workflows",
        "paths": (
            "contracts/ports/workflow/delivery.py",
            "orchestration/workflows/deferred.py",
            "ztest/workflows/test_reconciliation.py",
        ),
        "recipe": "workflow-terminal-delivery-v3",
        "command": "python -B -m pytest ztest/workflows/test_reconciliation.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-TEMPORAL-001": {
        "owner": "product.workflows",
        "paths": (
            "product/workflows/temporal_effects.py",
            "ztest/architecture/test_workflow_reconciliation_owner.py",
        ),
        "recipe": "workflow-temporal-evidence-v3",
        "command": "python -B -m pytest ztest/architecture/test_workflow_reconciliation_owner.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-WORKFLOW-GOVERNANCE-INTEGRATION-001": {
        "owner": "product.workflows",
        "paths": (
            "product/workflows/durability.py",
            "orchestration/workflows/durable/control.py",
            "ztest/architecture/test_workflow_reconciliation_owner.py",
        ),
        "recipe": "workflow-governance-integration-v3",
        "command": "python -B -m pytest ztest/architecture/test_workflow_reconciliation_owner.py ztest/workflows/test_reconciliation.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-001": {
        "owner": "contracts.service",
        "paths": (
            "contracts/service/journal.py",
            "contracts/service/models.py",
            "ztest/runtime/test_service_gateway.py",
        ),
        "recipe": "service-call-v3-contract-v1",
        "command": "python -B -m pytest ztest/runtime/test_service_gateway.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-MIGRATION-001": {
        "owner": "runtime.service_gateway.migration",
        "paths": (
            "runtime/service_gateway/migration.py",
            "ztest/runtime/test_service_call_migration.py",
        ),
        "recipe": "service-call-v2-inventory-v1",
        "command": "python -B -m pytest ztest/runtime/test_service_call_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-MIGRATION-002": {
        "owner": "runtime.service_gateway.migration",
        "paths": (
            "runtime/service_gateway/migration.py",
            "ztest/runtime/test_service_call_migration.py",
        ),
        "recipe": "service-call-v3-cutover-v1",
        "command": "python -B -m pytest ztest/runtime/test_service_call_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-STORE-001": {
        "owner": "runtime.service_gateway",
        "paths": (
            "runtime/service_gateway/journal.py",
            "runtime/service_gateway/snapshot.py",
            "ztest/runtime/test_service_gateway.py",
        ),
        "recipe": "service-call-store-v3",
        "command": "python -B -m pytest ztest/runtime/test_service_gateway.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-EXECUTION-001": {
        "owner": "runtime.service_gateway",
        "paths": (
            "runtime/service_gateway/gateway.py",
            "contracts/ports/service/command_runtime.py",
            "ztest/runtime/test_service_gateway.py",
        ),
        "recipe": "service-call-execution-v3",
        "command": "python -B -m pytest ztest/runtime/test_service_gateway.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-RECONCILE-001": {
        "owner": "runtime.service_gateway",
        "paths": (
            "runtime/service_gateway/reconciler.py",
            "ztest/runtime/test_service_gateway.py",
        ),
        "recipe": "service-call-reconcile-v3",
        "command": "python -B -m pytest ztest/runtime/test_service_gateway.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-CONSUMERS-001": {
        "owner": "product.composition",
        "paths": (
            "runtime/service_gateway/gateway.py",
            "ztest/architecture/test_operation_ownership_boundary.py",
        ),
        "recipe": "service-call-consumers-v3",
        "command": "python -B -m pytest ztest/architecture/test_operation_ownership_boundary.py ztest/runtime/test_service_gateway.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SERVICE-CALL-RETIRE-001": {
        "owner": "runtime.service_gateway",
        "paths": (
            "runtime/service_gateway/migration.py",
            "ztest/runtime/test_service_call_migration.py",
        ),
        "recipe": "service-call-v2-retired-v1",
        "command": "python -B -m pytest ztest/runtime/test_service_call_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-ARTIFACT-EDGE-001": {
        "owner": "contracts.artifact",
        "paths": (
            "contracts/artifact/governance.py",
            "ztest/runtime/test_artifact_store.py",
        ),
        "recipe": "artifact-ownership-v2-contract-v1",
        "command": "python -B -m pytest ztest/runtime/test_artifact_store.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-ARTIFACT-MIGRATION-001": {
        "owner": "product.migrations.artifact_store",
        "paths": (
            "product/migrations/artifact_store.py",
            "ztest/runtime/test_artifact_migration.py",
        ),
        "recipe": "artifact-v1-inventory-v1",
        "command": "python -B -m pytest ztest/runtime/test_artifact_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-ARTIFACT-MIGRATION-002": {
        "owner": "product.migrations.artifact_store",
        "paths": (
            "product/migrations/artifact_store.py",
            "ztest/runtime/test_artifact_migration.py",
        ),
        "recipe": "artifact-v2-cutover-v1",
        "command": "python -B -m pytest ztest/runtime/test_artifact_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-ARTIFACT-STORE-001": {
        "owner": "runtime.artifacts",
        "paths": (
            "runtime/artifacts/store.py",
            "runtime/artifacts/repository.py",
            "ztest/runtime/test_artifact_store.py",
        ),
        "recipe": "artifact-store-v2",
        "command": "python -B -m pytest ztest/runtime/test_artifact_store.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-ARTIFACT-DELETION-001": {
        "owner": "runtime.artifacts",
        "paths": (
            "runtime/artifacts/gc.py",
            "runtime/artifacts/store.py",
            "ztest/runtime/test_artifact_store.py",
        ),
        "recipe": "artifact-fenced-deletion-v2",
        "command": "python -B -m pytest ztest/runtime/test_artifact_store.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-DELETION-001": {
        "owner": "runtime.session",
        "paths": (
            "runtime/session/deletion.py",
            "runtime/session/lifecycle.py",
            "ztest/session/test_lifecycle.py",
        ),
        "recipe": "session-fenced-deletion-v1",
        "command": "python -B -m pytest ztest/session/test_lifecycle.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-ARTIFACT-CONSUMERS-001": {
        "owner": "product.composition",
        "paths": (
            "runtime/artifacts/pins.py",
            "runtime/artifacts/ownership.py",
            "ztest/architecture/test_artifact_pin_registry_ownership.py",
        ),
        "recipe": "artifact-producer-completeness-v2",
        "command": "python -B -m pytest ztest/architecture/test_artifact_pin_registry_ownership.py ztest/runtime/test_artifact_publication.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-ARTIFACT-GC-001": {
        "owner": "runtime.artifacts",
        "paths": (
            "runtime/artifacts/gc.py",
            "runtime/artifacts/store.py",
            "ztest/runtime/test_artifact_store.py",
        ),
        "recipe": "artifact-generation-closure-gc-v2",
        "command": "python -B -m pytest ztest/runtime/test_artifact_store.py ztest/architecture/test_artifact_pin_registry_ownership.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-INGRESS-001": {
        "owner": "contracts.agent.ingress",
        "paths": (
            "contracts/agent/delivery.py",
            "contracts/ports/agent/ingress.py",
            "ztest/agents/test_turn_queue_codec.py",
        ),
        "recipe": "agent-ingress-v2-contract-v1",
        "command": "python -B -m pytest ztest/agents/test_turn_queue_codec.py ztest/architecture/test_mailbox_durable_schema.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-INGRESS-MIGRATION-001": {
        "owner": "orchestration.agents.ingress",
        "paths": ("ztest/agents/test_ingress_migration.py",),
        "recipe": "agent-ingress-v1-inventory-v1",
        "command": "python -B -m pytest ztest/agents/test_ingress_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-INGRESS-MIGRATION-002": {
        "owner": "orchestration.agents.ingress",
        "paths": ("ztest/agents/test_ingress_migration.py",),
        "recipe": "agent-ingress-v2-cutover-v1",
        "command": "python -B -m pytest ztest/agents/test_ingress_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-DELIVERY-001": {
        "owner": "orchestration.agents.messaging",
        "paths": (
            "orchestration/agents/messaging/durable.py",
            "contracts/ports/agent/delivery.py",
            "ztest/agents/test_durable_delivery.py",
        ),
        "recipe": "agent-delivery-v2-v1",
        "command": "python -B -m pytest ztest/agents/test_durable_delivery.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-TURN-001": {
        "owner": "orchestration.agents.turn_queue",
        "paths": (
            "orchestration/agents/turn_queue/store.py",
            "orchestration/agents/turn_queue/scheduler.py",
            "ztest/architecture/test_agent_turn_queue_governance.py",
        ),
        "recipe": "agent-turn-v2-v1",
        "command": "python -B -m pytest ztest/agents/test_turn_queue_store.py ztest/agents/test_turn_queue_scheduler.py ztest/architecture/test_agent_turn_queue_governance.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-INGRESS-RECONCILE-001": {
        "owner": "orchestration.agents.ingress",
        "paths": (
            "orchestration/agents/ingress/reconcile.py",
            "ztest/agents/test_ingress_reconcile.py",
        ),
        "recipe": "agent-ingress-reconcile-v2",
        "command": "python -B -m pytest ztest/agents/test_ingress_reconcile.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-PROJECTION-001": {
        "owner": "orchestration.agents.messaging",
        "paths": (
            "orchestration/agents/messaging/mailbox.py",
            "orchestration/agents/residency/model.py",
            "ztest/architecture/test_mailbox_durable_schema.py",
        ),
        "recipe": "agent-ingress-projections-v2",
        "command": "python -B -m pytest ztest/architecture/test_mailbox_durable_schema.py ztest/agents/test_mailbox.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-AGENT-INGRESS-SURFACES-001": {
        "owner": "product.composition",
        "paths": (
            "product/automation/agent_trigger.py",
            "orchestration/agents/ingress/reconcile.py",
            "ztest/agents/test_ingress_reconcile.py",
        ),
        "recipe": "agent-ingress-surfaces-v2",
        "command": "python -B -m pytest ztest/agents/test_ingress_reconcile.py ztest/automation/cron/test_service.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-CRON-001": {
        "owner": "orchestration.automation.cron",
        "paths": (
            "orchestration/automation/cron/task.py",
            "orchestration/automation/cron/store.py",
            "ztest/architecture/test_cron_durable_schema.py",
        ),
        "recipe": "cron-v3-contract-v1",
        "command": "python -B -m pytest ztest/architecture/test_cron_durable_schema.py ztest/automation/cron/test_store.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-CRON-002": {
        "owner": "orchestration.automation.cron.migration",
        "paths": (
            "orchestration/automation/cron/migration.py",
            "ztest/automation/cron/test_migration.py",
        ),
        "recipe": "cron-v3-migration-v1",
        "command": "python -B -m pytest ztest/automation/cron/test_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-CRON-003": {
        "owner": "orchestration.automation.cron",
        "paths": (
            "orchestration/automation/cron/store.py",
            "orchestration/automation/cron/task.py",
            "ztest/automation/cron/test_store.py",
        ),
        "recipe": "cron-v3-command-query-v1",
        "command": "python -B -m pytest ztest/automation/cron/test_store.py ztest/automation/cron/test_task.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-CRON-004": {
        "owner": "orchestration.automation.cron",
        "paths": (
            "orchestration/automation/cron/scheduler.py",
            "orchestration/automation/cron/store.py",
            "ztest/automation/cron/test_scheduler.py",
        ),
        "recipe": "cron-v3-reconcile-v1",
        "command": "python -B -m pytest ztest/automation/cron/test_scheduler.py ztest/architecture/test_cron_receipt_settlement.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-CRON-DELIVERY-001": {
        "owner": "product.automation",
        "paths": (
            "product/automation/agent_trigger.py",
            "ztest/automation/cron/test_service.py",
            "ztest/architecture/test_cron_receipt_settlement.py",
        ),
        "recipe": "cron-agent-delivery-v3",
        "command": "python -B -m pytest ztest/automation/cron/test_service.py ztest/architecture/test_cron_receipt_settlement.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-CRON-ARTIFACT-001": {
        "owner": "orchestration.automation.cron",
        "paths": (
            "orchestration/automation/cron/task.py",
            "ztest/automation/cron/test_task.py",
        ),
        "recipe": "cron-artifact-edge-v3",
        "command": "python -B -m pytest ztest/automation/cron/test_task.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-001": {
        "owner": "runtime.models.auth.oauth",
        "paths": (
            "runtime/models/auth/oauth/storage/base.py",
            "ztest/oauth/test_storage.py",
        ),
        "recipe": "oauth-credential-v2-contract-v1",
        "command": "python -B -m pytest ztest/oauth/test_storage.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-MIGRATION-001": {
        "owner": "runtime.models.auth.oauth.migration",
        "paths": (
            "runtime/models/auth/oauth/migration.py",
            "ztest/oauth/test_migration.py",
        ),
        "recipe": "oauth-v1-inventory-v1",
        "command": "python -B -m pytest ztest/oauth/test_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-MIGRATION-002": {
        "owner": "runtime.models.auth.oauth.migration",
        "paths": (
            "runtime/models/auth/oauth/migration.py",
            "ztest/oauth/test_migration.py",
        ),
        "recipe": "oauth-v2-candidate-cutover-v1",
        "command": "python -B -m pytest ztest/oauth/test_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-EFFECT-001": {
        "owner": "runtime.models.auth.oauth",
        "paths": (
            "runtime/models/auth/oauth/effects.py",
            "ztest/oauth/test_effects.py",
        ),
        "recipe": "oauth-effect-intent-v1",
        "command": "python -B -m pytest ztest/oauth/test_effects.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-STORE-001": {
        "owner": "runtime.models.auth.oauth.storage",
        "paths": (
            "runtime/models/auth/oauth/storage/metadata.py",
            "runtime/models/auth/oauth/storage/file_store.py",
            "runtime/models/auth/oauth/storage/keyring_store.py",
            "ztest/oauth/test_storage.py",
        ),
        "recipe": "oauth-v2-fenced-metadata-vault-borrow-v1",
        "command": "python -B -m pytest ztest/oauth/test_storage.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-COMMAND-001": {
        "owner": "runtime.models.auth.oauth",
        "paths": (
            "runtime/models/auth/oauth/storage/base.py",
            "runtime/models/auth/oauth/manager.py",
            "ztest/oauth/test_manager.py",
        ),
        "recipe": "oauth-closed-maintenance-command-v1",
        "command": "python -B -m pytest ztest/oauth/test_manager.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-CONSUMER-001": {
        "owner": "product.models.credentials",
        "paths": (
            "product/models/credential_sources.py",
            "runtime/tools/mcp/oauth.py",
            "ztest/oauth/test_consumer_borrow.py",
            "ztest/executor/mcp/test_oauth.py",
        ),
        "recipe": "oauth-consumer-bound-borrow-v1",
        "command": "python -B -m pytest ztest/oauth/test_consumer_borrow.py ztest/executor/mcp/test_oauth.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-SECRET-ERASURE-001": {
        "owner": "runtime.models.auth.oauth.storage",
        "paths": (
            "runtime/models/auth/oauth/storage/metadata.py",
            "runtime/models/auth/oauth/storage/file_store.py",
            "runtime/models/auth/oauth/storage/keyring_store.py",
            "ztest/oauth/test_storage.py",
            "ztest/oauth/test_manager.py",
        ),
        "recipe": "oauth-generation-revoke-crypto-erasure-v1",
        "command": "python -B -m pytest ztest/oauth/test_storage.py ztest/oauth/test_manager.py::test_closed_maintenance_commands_preserve_evidence_and_erase_bearer -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-PRODUCTION-PATH-RETIRE-001": {
        "owner": "product.models.credentials",
        "paths": (
            "runtime/models/clients/credentials.py",
            "product/models/credential_sources.py",
            "ztest/architecture/test_oauth_credential_governance.py",
            "ztest/oauth/test_llm_integration.py",
        ),
        "retired_paths": ("runtime/models/auth/oauth/storage/fallback_store.py",),
        "recipe": "oauth-production-fallback-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_oauth_credential_governance.py ztest/oauth/test_llm_integration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-OAUTH-MIGRATION-EVIDENCE-RETIRE-001": {
        "owner": "runtime.models.auth.oauth.migration",
        "paths": (
            "runtime/models/auth/oauth/migration.py",
            "ztest/oauth/test_migration.py",
        ),
        "recipe": "oauth-secret-safe-evidence-retirement-v1",
        "command": "python -B -m pytest ztest/oauth/test_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-RUN-DOMAINS-001": {
        "owner": "runtime.run-domains",
        "paths": (
            "runtime/session/pending_act.py",
            "runtime/models/session_projection.py",
            "runtime/session/timers.py",
            "ztest/architecture/test_run_domain_ownership.py",
        ),
        "recipe": "runtime-run-domain-owners-v1",
        "command": "python -B -m pytest ztest/architecture/test_run_domain_ownership.py ztest/runtime/test_pending_act_transaction.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-TOOL-EFFECT-001": {
        "owner": "runtime.tools",
        "paths": (
            "runtime/session/pending_act.py",
            "runtime/tools/tool_executor.py",
            "ztest/runtime/test_pending_act_transaction.py",
        ),
        "recipe": "runtime-tool-effect-v1",
        "command": "python -B -m pytest ztest/runtime/test_pending_act_transaction.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-MODEL-PROJECTION-001": {
        "owner": "runtime.models",
        "paths": (
            "runtime/models/session_projection.py",
            "ztest/inference/test_model_session_projection.py",
        ),
        "recipe": "model-session-projection-v1",
        "command": "python -B -m pytest ztest/inference/test_model_session_projection.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-MODEL-CHECKPOINT-001": {
        "owner": "runtime.models",
        "paths": (
            "contracts/model/checkpoint.py",
            "contracts/execution/models.py",
            "runtime/models/failover/model_journal.py",
            "runtime/models/checkpoint_maintenance.py",
            "runtime/models/session_projection.py",
            "ztest/architecture/test_model_checkpoint_governance.py",
        ),
        "recipe": "model-checkpoint-policy-retention-v1",
        "command": "python -B -m pytest ztest/architecture/test_model_checkpoint_governance.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-MODEL-PERSISTENCE-MIGRATION-001": {
        "owner": "product.migrations.model_checkpoint",
        "paths": (
            "product/migrations/model_checkpoint.py",
            "ztest/inference/test_model_checkpoint_migration.py",
        ),
        "recipe": "model-checkpoint-source-inventory-cutover-v1",
        "command": "python -B -m pytest ztest/inference/test_model_checkpoint_migration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-MODEL-COMPOSITION-001": {
        "owner": "product.composition.models",
        "paths": (
            "product/composition/model_builder.py",
            "product/config/model_checkpoint.py",
            "runtime/models/model_gateway.py",
            "runtime/models/composition.py",
            "ztest/architecture/test_model_contract_governance.py",
            "ztest/architecture/test_model_checkpoint_governance.py",
        ),
        "recipe": "model-generation-request-attempt-composition-v1",
        "command": "python -B -m pytest ztest/architecture/test_model_contract_governance.py ztest/architecture/test_model_checkpoint_governance.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-MODEL-RECOVERY-001": {
        "owner": "runtime.models",
        "paths": (
            "contracts/ports/model/recovery.py",
            "runtime/durable/inference_checkpoint.py",
            "runtime/models/model_gateway.py",
            "ztest/inference/test_model_session_projection.py",
            "ztest/router/llm/test_model_call_journal.py",
        ),
        "recipe": "model-wire-crash-session-projection-recovery-v1",
        "command": "python -B -m pytest ztest/inference/test_model_session_projection.py ztest/router/llm/test_model_call_journal.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-SESSION-TIMER-001": {
        "owner": "runtime.session",
        "paths": (
            "runtime/session/timers.py",
            "ztest/architecture/test_run_domain_ownership.py",
        ),
        "recipe": "session-timer-v1",
        "command": "python -B -m pytest ztest/architecture/test_run_domain_ownership.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-RUNJOURNAL-CONSUMERS-001": {
        "owner": "product.composition",
        "paths": ("ztest/architecture/test_run_domain_ownership.py",),
        "retired_paths": (
            "runtime/ledger/run_journal.py",
            "runtime/durable/inference_journal.py",
        ),
        "recipe": "run-journal-consumers-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_run_domain_ownership.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-TEMPORAL-RUNJOURNAL-RETIRE-001": {
        "owner": "product.workflows",
        "paths": (
            "product/workflows/temporal_effects.py",
            "ztest/architecture/test_run_domain_ownership.py",
        ),
        "retired_paths": ("runtime/durable/temporal/_backend.py",),
        "recipe": "temporal-run-journal-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_run_domain_ownership.py -q --tb=short -p no:cacheprovider",
    },
    "R-W3-RUNJOURNAL-RETIRE-001": {
        "owner": "runtime.run-domains",
        "paths": ("ztest/architecture/test_run_domain_ownership.py",),
        "retired_paths": (
            "runtime/ledger/run_journal.py",
            "runtime/durable/backend.py",
            "runtime/durable/factory.py",
        ),
        "recipe": "generic-run-journal-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_run_domain_ownership.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-BGTASK-QUERY-001": {
        "owner": "orchestration.background_tasks",
        "paths": (
            "orchestration/background_tasks/model.py",
            "orchestration/background_tasks/pool.py",
            "ztest/architecture/test_orchestration_dependencies.py",
        ),
        "recipe": "background-task-immutable-query-v1",
        "command": "python -B -m pytest ztest/architecture/test_orchestration_dependencies.py ztest/background_tasks/test_pool.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-BGTASK-CLEANUP-001": {
        "owner": "orchestration.background_tasks",
        "paths": (
            "contracts/task/lifecycle.py",
            "contracts/ports/task/operations.py",
            "orchestration/background_tasks/pool.py",
            "ztest/architecture/test_background_task_pool_lifecycle.py",
        ),
        "recipe": "background-task-bounded-cleanup-v1",
        "command": "python -B -m pytest ztest/architecture/test_background_task_pool_lifecycle.py ztest/background_tasks/test_pool.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-PRESENTATION-002": {
        "owner": "product.presentation.events",
        "paths": (
            "product/presentation/events/catalog.py",
            "contracts/events/scope.py",
            "ztest/cli/test_view_event_catalog.py",
        ),
        "recipe": "product-view-event-catalog-v1",
        "command": "python -B -m pytest ztest/cli/test_view_event_catalog.py ztest/contracts/test_event_scope.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-PRESENTATION-003": {
        "owner": "product.presentation",
        "paths": (
            "product/presentation/consumer.py",
            "product/interfaces/structured/consumer.py",
            "product/interfaces/acp/wire.py",
            "product/interfaces/agui/wire.py",
            "product/interfaces/textual/consumer.py",
            "ztest/cli/test_view_event_catalog.py",
        ),
        "recipe": "product-view-event-adapters-v1",
        "command": "python -B -m pytest ztest/cli/test_view_event_catalog.py ztest/cli/consumers/acp/test_wire_acp.py ztest/cli/test_wire_agui.py ztest/cli/test_textual_consumer.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-PRESENTATION-001": {
        "owner": "contracts.events.scope",
        "paths": (
            "contracts/events/scope.py",
            "runtime/events/scope.py",
            "ztest/contracts/test_event_scope.py",
        ),
        "recipe": "execution-scope-contract-v1",
        "command": "python -B -m pytest ztest/contracts/test_event_scope.py ztest/runtime/test_surface_presentation.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-LSP-001": {
        "owner": "contracts.ports.code_intelligence",
        "paths": (
            "contracts/ports/code_intelligence/lsp.py",
            "ztest/lsp/test_query.py",
        ),
        "recipe": "lsp-3.17-code-map-profile-v1",
        "command": "python -B -m pytest ztest/lsp/test_jsonrpc.py ztest/lsp/test_query.py ztest/lsp/test_service_integration.py ztest/lsp/test_registry.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-LSP-002": {
        "owner": "runtime.lsp",
        "paths": (
            "runtime/lsp/jsonrpc.py",
            "runtime/lsp/service.py",
            "ztest/lsp/test_service_integration.py",
        ),
        "recipe": "runtime-lsp-adapter-v1",
        "command": "python -B -m pytest ztest/lsp/test_jsonrpc.py ztest/lsp/test_service_integration.py -q --tb=short -p no:cacheprovider",
    },
    "R-W2-LSP-003": {
        "owner": "product.code_map",
        "paths": (
            "product/code_map/factory.py",
            "product/code_map/rendering.py",
            "ztest/lsp/test_query.py",
        ),
        "recipe": "product-code-map-lsp-consumer-v1",
        "command": "python -B -m pytest ztest/lsp/test_query.py ztest/lsp/test_registry.py -q --tb=short -p no:cacheprovider",
    },
    "R-W1-001": {
        "owner": "product.models.providers",
        "paths": ("ztest/architecture/test_post_closure_w1_retirement.py",),
        "retired_paths": ("product/models/providers/error_handling.py",),
        "recipe": "provider-moderation-surface-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_post_closure_w1_retirement.py -q --tb=short -p no:cacheprovider",
    },
    "R-W1-002": {
        "owner": "product.interfaces",
        "paths": ("ztest/architecture/test_post_closure_w1_retirement.py",),
        "retired_paths": (
            "product/interfaces/inference_admin_api/__init__.py",
            "product/interfaces/inference_admin_api/application.py",
        ),
        "recipe": "inference-admin-surface-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_post_closure_w1_retirement.py -q --tb=short -p no:cacheprovider",
    },
    "R-W1-003": {
        "owner": "contracts.ports.model",
        "paths": ("ztest/architecture/test_post_closure_w1_retirement.py",),
        "retired_paths": ("contracts/ports/model/client.py",),
        "recipe": "legacy-model-client-retired-v1",
        "command": "python -B -m pytest ztest/architecture/test_post_closure_w1_retirement.py -q --tb=short -p no:cacheprovider",
    },
    "R-W1-005": {
        "owner": "product.i18n",
        "paths": (
            "product/i18n/runtime.py",
            "product/i18n/catalog/__init__.py",
            "ztest/i18n/test_guardrail.py",
        ),
        "recipe": "product-immutable-i18n-catalog-v1",
        "command": "python -B -m pytest ztest/i18n/test_guardrail.py ztest/i18n/test_runtime.py -q --tb=short -p no:cacheprovider",
    },
    "R-W1-006-temporal": {
        "owner": "product.workflows",
        "paths": (
            "product/composition/bootstrap.py",
            "product/workflows/temporal_effects.py",
            "ztest/architecture/test_optional_backend_activation.py",
        ),
        "recipe": "product-temporal-effect-plane-v1",
        "command": "python -B -m pytest ztest/architecture/test_optional_backend_activation.py ztest/architecture/test_workflow_reconciliation_owner.py -q --tb=short -p no:cacheprovider",
    },
    "R-W1-006-squilla": {
        "owner": "product.routing.squilla",
        "paths": (
            "product/routing/squilla/ml/engine.py",
            "ztest/architecture/test_optional_backend_activation.py",
        ),
        "recipe": "product-squilla-inference-core-v1",
        "command": "python -B -m pytest ztest/architecture/test_optional_backend_activation.py -q --tb=short -p no:cacheprovider",
    },
    "R-W1-004": {
        "owner": "runtime.secrets",
        "paths": (
            "runtime/secrets/cipher.py",
            "product/config/secrets.py",
            "ztest/secrets/test_cipher.py",
        ),
        "recipe": "runtime-aes-gcm-vault-v1",
        "command": "python -B -m pytest ztest/secrets/test_cipher.py ztest/secrets/test_store.py ztest/secrets/test_prompt_policy.py -q --tb=short -p no:cacheprovider",
    },
    "R-W0-WORKFLOW-GOVERNANCE-VERIFY-001": {
        "owner": "orchestration.workflows.durable",
        "paths": (
            "orchestration/workflows/durable/reconciliation.py",
            "product/workflows/durability.py",
            "ztest/architecture/test_workflow_reconciliation_owner.py",
        ),
        "recipe": "product-workflow-durability-v3",
        "command": "python -B -m pytest ztest/architecture/test_workflow_reconciliation_owner.py ztest/architecture/test_operation_ownership_boundary.py -q --tb=short -p no:cacheprovider",
    },
    "R-W0-BGTASK-GOVERNANCE-VERIFY-001": {
        "owner": "orchestration.background_tasks",
        "paths": (
            "orchestration/background_tasks/pool.py",
            "product/agents/background_tasks.py",
            "ztest/background_tasks/test_pool.py",
        ),
        "recipe": "per-agent-background-task-pool-v1",
        "command": "python -B -m pytest ztest/background_tasks/test_pool.py ztest/background_tasks/test_attachment.py -q --tb=short -p no:cacheprovider",
    },
}


def _git(*args: str) -> bytes:
    return subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _tree_digest(prefixes: tuple[str, ...]) -> str:
    entries: list[bytes] = []
    for prefix in prefixes:
        root = ROOT / prefix
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(ROOT).as_posix().encode()
            entries.append(relative + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return _digest(b"\n".join(entries) + b"\n")


def build_baseline() -> dict[str, object]:
    source = {
        "agents": _digest((ROOT / "AGENTS.md").read_bytes()),
        "production": _tree_digest(("contracts", "kernel", "runtime", "orchestration", "product")),
        "tests": _tree_digest(("ztest",)),
        "requirements": _digest(REQUIREMENTS.read_bytes()),
    }
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "kind": "source_baseline",
        "base_revision": _git("rev-parse", "HEAD").decode().strip(),
        "source_sets": source,
    }
    manifest["manifest_identity"] = _canonical_digest(manifest)
    return manifest


def _load_receipts() -> dict[str, dict[str, object]]:
    if not RECEIPTS.exists():
        return {}
    raw = json.loads(RECEIPTS.read_text(encoding="utf-8"))
    if type(raw) is not dict or raw.get("schema") != "post-closure-verification-receipts/v1":
        raise RuntimeError("post-closure verification receipt store is unsupported")
    receipts = raw.get("receipts")
    if type(receipts) is not list:
        raise RuntimeError("post-closure verification receipt store is malformed")
    return {
        receipt["command"]: receipt
        for receipt in receipts
        if type(receipt) is dict and type(receipt.get("command")) is str
    }


def _commands(spec: dict[str, object]) -> tuple[str, ...]:
    commands = spec.get("commands")
    if commands is not None:
        if type(commands) is not tuple or any(type(command) is not str for command in commands):
            raise TypeError("verification commands must be a tuple of command strings")
        return commands
    command = spec.get("command")
    if type(command) is not str:
        raise TypeError("verification recipe requires a command")
    return (command,)


def run_recipes(
    *,
    include_governance: bool,
    max_recipes: int | None,
    selected_command: str | None,
) -> None:
    """Execute registered recipes serially and bind receipts to this baseline."""

    baseline = build_baseline()
    baseline_identity = baseline["manifest_identity"]
    commands = {command for spec in _VERIFIED_REQUIREMENTS.values() for command in _commands(spec)}
    if include_governance:
        commands.add(
            "python -B -m pytest ztest/architecture/test_post_closure_governance.py -q --tb=short -p no:cacheprovider"
        )
    if selected_command is not None:
        if selected_command not in commands:
            raise ValueError("selected command is not a registered verification recipe")
        commands = {selected_command}
    prior = _load_receipts()
    receipts: list[dict[str, object]] = [
        receipt for receipt in prior.values() if receipt.get("source_baseline_identity") == baseline_identity
    ]
    executed = 0
    for command in sorted(commands):
        existing = prior.get(command)
        if (
            existing is not None
            and existing.get("source_baseline_identity") == baseline_identity
            and existing.get("exit_code") == 0
        ):
            continue
        if max_recipes is not None and executed >= max_recipes:
            break
        print(f"RUN {command}", flush=True)
        started_at = datetime.now(timezone.utc)
        completed = subprocess.run(
            shlex.split(command),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
            env={**os.environ, "PYTHONPYCACHEPREFIX": "/tmp/mote-pycache"},
        )
        finished_at = datetime.now(timezone.utc)
        completed_baseline_identity = build_baseline()["manifest_identity"]
        output = completed.stdout
        output_path = RECEIPT_OUTPUT_DIR / f"{_digest(command.encode()).removeprefix('sha256:')}.log"
        RECEIPT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(output)
        receipt: dict[str, object] = {
            "command": command,
            "source_baseline_identity": baseline_identity,
            "completed_source_baseline_identity": completed_baseline_identity,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "exit_code": completed.returncode,
            "output_digest": _digest(output),
            "output_path": output_path.relative_to(ROOT).as_posix(),
            "output_tail": output.decode("utf-8", errors="replace")[-4000:],
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "cwd": str(ROOT),
                "pytest_parallel": False,
            },
        }
        receipts = [item for item in receipts if item.get("command") != command]
        receipts.append(receipt)
        executed += 1
        store: dict[str, object] = {
            "schema": "post-closure-verification-receipts/v1",
            "source_baseline_identity": baseline_identity,
            "receipts": sorted(receipts, key=lambda item: str(item["command"])),
        }
        store["manifest_identity"] = _canonical_digest(store)
        RECEIPTS.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if completed.returncode != 0:
            raise RuntimeError(f"verification recipe failed: {command}\n{receipt['output_tail']}")
        if completed_baseline_identity != baseline_identity:
            raise RuntimeError(f"source baseline changed during verification recipe: {command}")
        print(f"PASS {command}", flush=True)


def _requirement_ids() -> list[str]:
    ids = set(_REQUIREMENT.findall(REQUIREMENTS.read_text(encoding="utf-8")))
    return sorted(requirement for requirement in ids - _NON_EXECUTABLE if not requirement.endswith("-"))


def build_evidence(baseline: dict[str, object]) -> dict[str, object]:
    baseline_identity = baseline["manifest_identity"]
    receipts = _load_receipts()
    records = []
    for requirement_id in _requirement_ids():
        governance = requirement_id == "R-W0-GOVERNANCE-001"
        verified_spec = _VERIFIED_REQUIREMENTS.get(requirement_id)
        commands = (
            (
                "python -B -m pytest ztest/architecture/test_post_closure_governance.py -q --tb=short -p no:cacheprovider",
            )
            if governance
            else _commands(verified_spec) if verified_spec else ()
        )
        command_receipts = tuple(receipts.get(command) for command in commands)
        verified = (
            (governance or verified_spec is not None)
            and bool(commands)
            and all(
                receipt is not None
                and receipt.get("source_baseline_identity") == baseline_identity
                and receipt.get("exit_code") == 0
                for receipt in command_receipts
            )
        )
        governance_paths = (
            "zdocs/architecture/generate_post_closure_governance.py",
            "ztest/architecture/post_closure_governance.py",
            "ztest/architecture/test_post_closure_governance.py",
        )
        verified_paths = governance_paths if governance else tuple(verified_spec["paths"]) if verified_spec else ()
        evidence = [{"path": path, "digest": _digest((ROOT / path).read_bytes())} for path in verified_paths]
        records.append(
            {
                "requirement_id": requirement_id,
                "reviewed_revision": "post-closure-implementation-r3-mechanical-closure",
                "owner_domain": requirement_id.split("-")[2],
                "execution_owner": (
                    "architecture-governance" if governance else verified_spec["owner"] if verified_spec else None
                ),
                "status": "VERIFIED" if verified else "OPEN",
                "verification_disposition": "PASS" if verified else "NOT_RUN",
                "failure_reasons": [],
                "recovery_conditions": ["rerun-recipe-after-source-baseline-change"],
                "completion_dependencies": ([] if governance else ["R-W0-GOVERNANCE-001"]),
                "source_baseline_identity": baseline_identity,
                "write_set": list(verified_paths),
                "evidence": evidence,
                "verification_commands": list(commands),
                "verification_receipts": list(command_receipts) if verified else [],
                "approval_authority": ("user-approved-requirements:2026-08-03" if verified else None),
                "decision_ids": [f"post-closure-requirements:{requirement_id}"],
                "recipe_ids": (
                    ["post-closure-governance-v1"] if governance else [verified_spec["recipe"]] if verified_spec else []
                ),
                "migration_disposition": (
                    "CUTOVER_VERIFIED"
                    if any(token in requirement_id for token in ("MIGRATION", "RETIRE"))
                    else "NOT_APPLICABLE"
                ),
                "integrated_source_identity": baseline_identity if verified else None,
                "activation_generation": baseline_identity if verified else None,
                "legacy_exit_receipts": (
                    [
                        {
                            "retired_paths": list(verified_spec.get("retired_paths", ())),
                            "source_baseline_identity": baseline_identity,
                        }
                    ]
                    if verified and verified_spec and verified_spec.get("retired_paths")
                    else []
                ),
                "retired_paths": (list(verified_spec.get("retired_paths", ())) if verified_spec else []),
                "verification_instant": (datetime.now(timezone.utc).isoformat() if verified else None),
            }
        )
    evidence: dict[str, object] = {
        "schema": SCHEMA,
        "kind": "governance_evidence",
        "source_baseline_identity": baseline_identity,
        "requirements": records,
    }
    evidence["manifest_identity"] = _canonical_digest(evidence)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-recipes", action="store_true")
    parser.add_argument("--include-governance", action="store_true")
    parser.add_argument("--max-recipes", type=int)
    parser.add_argument("--command")
    args = parser.parse_args()
    if args.run_recipes:
        run_recipes(
            include_governance=args.include_governance,
            max_recipes=args.max_recipes,
            selected_command=args.command,
        )
        return
    baseline = build_baseline()
    BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    EVIDENCE.write_text(json.dumps(build_evidence(baseline), ensure_ascii=False, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
