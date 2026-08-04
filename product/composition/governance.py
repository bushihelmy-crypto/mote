"""Authoritative declarations for governed production roots and capabilities."""

from __future__ import annotations

from mote.contracts.composition import (
    CandidateClassification,
    CandidateRole,
    CapabilityDeclaration,
    DeploymentMode,
    Enablement,
    FacadeDeclaration,
    FacadeStatus,
    InstanceScope,
    OwnerDeclaration,
    PublicSymbolClassification,
    PublicSymbolRole,
)
from mote.contracts.events.governance import WireAuthorityDeclaration, WireAuthorityKind

CLASSIFIER_VERSION = "product-composition-v1"

WIRE_AUTHORITY_DECLARATIONS = (
    WireAuthorityDeclaration(
        api_id="inference-shared-grpc",
        protocol_generation=1,
        authority_kind=WireAuthorityKind.CONTRACT_FIRST,
        authority_path="zdocs/parity/rpc/gateway-v1.proto",
        generated_outputs=(
            "product/inference/daemon/rpc/gateway_v1_pb2.py",
            "product/inference/daemon/rpc/gateway_v1_pb2_grpc.py",
        ),
        conformance_checker="ztest/architecture/wire_authority.py",
        owner_id="inference-daemon",
    ),
    WireAuthorityDeclaration(
        api_id="inference-http",
        protocol_generation=1,
        authority_kind=WireAuthorityKind.CODE_FIRST,
        authority_path="zdocs/parity/api/inference-v1.openapi.yaml",
        generated_outputs=(),
        conformance_checker="zdocs/parity/freeze_idl_baseline.py",
        owner_id="inference-daemon",
    ),
    WireAuthorityDeclaration(
        api_id="inference-admin-http",
        protocol_generation=1,
        authority_kind=WireAuthorityKind.CODE_FIRST,
        authority_path="zdocs/parity/api/admin-v1.openapi.yaml",
        generated_outputs=(),
        conformance_checker="zdocs/parity/freeze_idl_baseline.py",
        owner_id="inference-daemon",
    ),
    WireAuthorityDeclaration(
        api_id="inference-realtime-webhook",
        protocol_generation=1,
        authority_kind=WireAuthorityKind.CODE_FIRST,
        authority_path="zdocs/parity/api/realtime-webhook-v1.asyncapi.yaml",
        generated_outputs=(),
        conformance_checker="zdocs/parity/freeze_idl_baseline.py",
        owner_id="inference-daemon",
    ),
)

PRODUCT_APPLICATION_ROOT = "mote.product.composition.bootstrap.activate_application"
DAEMON_ROOT = "mote.product.inference.daemon.application.SharedDaemonApplication"
TEXTUAL_ROOT = "mote.product.interfaces.textual.bootstrap.run_textual"
AGUI_ROOT = "mote.product.interfaces.agui.server.create_app"
ACP_ROOT = "mote.product.interfaces.acp.server.serve"

OWNER_DECLARATIONS = (
    OwnerDeclaration("contracts", "contracts"),
    OwnerDeclaration("kernel", "kernel"),
    OwnerDeclaration("runtime", "runtime"),
    OwnerDeclaration("orchestration", "orchestration"),
    OwnerDeclaration("product", "product"),
    OwnerDeclaration("product-composition", "product/composition"),
    OwnerDeclaration("product-cli", "product/entrypoints/cli"),
    OwnerDeclaration("inference-daemon", "product/inference/daemon"),
    OwnerDeclaration("textual-interface", "product/interfaces/textual"),
    OwnerDeclaration("agui-interface", "product/interfaces/agui"),
    OwnerDeclaration("acp-interface", "product/interfaces/acp"),
)

FACADE_DECLARATIONS = (
    FacadeDeclaration(
        facade_id="product.application",
        symbol="mote.product.Application",
        defining_symbol="mote.product.composition.application.Application",
        owner_id="product-composition",
        status=FacadeStatus.CANONICAL,
    ),
    FacadeDeclaration(
        facade_id="product.container",
        symbol="mote.product.ProductContainer",
        defining_symbol="mote.product.composition.container.ProductContainer",
        owner_id="product-composition",
        status=FacadeStatus.CANONICAL,
    ),
)


def _session_capability(
    capability_id: str,
    implementation: str,
    factory: str,
    *,
    enablement: Enablement = Enablement.REQUIRED,
    required_ports: tuple[str, ...] = (),
) -> CapabilityDeclaration:
    return CapabilityDeclaration(
        capability_id=capability_id,
        implementation=implementation,
        implementation_owner="runtime",
        applicable_root=PRODUCT_APPLICATION_ROOT,
        enablement=enablement,
        canonical_factory=factory,
        required_ports=required_ports,
        deployment_mode=DeploymentMode.EMBEDDED,
        instance_scope=InstanceScope.SESSION,
        lifecycle_owner="runtime-role",
        start_owner="runtime-role",
        stop_owner="runtime-role",
    )


CAPABILITY_DECLARATIONS = (
    CapabilityDeclaration(
        capability_id="model-runtime",
        implementation="mote.runtime.models.composition.SharedRuntimeCompositionHandle",
        implementation_owner="runtime",
        applicable_root=PRODUCT_APPLICATION_ROOT,
        enablement=Enablement.REQUIRED,
        canonical_factory="mote.product.composition.model_builder.build_application_candidate",
        required_ports=("mote.contracts.ports.model",),
        deployment_mode=DeploymentMode.EMBEDDED,
        instance_scope=InstanceScope.APPLICATION,
        lifecycle_owner="product-composition",
        start_owner="product-composition",
        stop_owner="product-composition",
    ),
    _session_capability(
        "telemetry",
        "mote.runtime.events.telemetry.TelemetryRuntime",
        "mote.runtime.agent.role_components._build_telemetry",
    ),
    _session_capability(
        "event-fabric",
        "mote.runtime.events.fabric.EventFabric",
        "mote.runtime.agent.components.session._build_event_fabric",
    ),
    _session_capability(
        "session-persistence",
        "mote.runtime.session.log.SessionLog",
        "mote.runtime.agent.components.session._build_session_log",
    ),
    _session_capability(
        "artifact-store",
        "mote.runtime.artifacts.DurableArtifactStore",
        "mote.runtime.agent.components.session._build_artifact_store",
        required_ports=("mote.contracts.ports.artifact.store.ArtifactStore",),
    ),
    _session_capability(
        "journal",
        "mote.runtime.events.journal.LocalEventJournal",
        "mote.runtime.session.log.SessionLog",
        required_ports=("mote.contracts.ports.events.journal.EventJournal",),
    ),
    _session_capability(
        "tool-executor",
        "mote.runtime.tools.tool_executor.ToolExecutor",
        "mote.runtime.agent.components.action._build_executor",
    ),
    CapabilityDeclaration(
        capability_id="tool-catalog",
        implementation="mote.runtime.tools.tool_registry.ToolCatalog",
        implementation_owner="runtime",
        applicable_root=PRODUCT_APPLICATION_ROOT,
        enablement=Enablement.REQUIRED,
        canonical_factory="mote.product.toolsets.builtin_tool_catalog",
        required_ports=(),
        deployment_mode=DeploymentMode.EMBEDDED,
        instance_scope=InstanceScope.APPLICATION,
        lifecycle_owner="product-composition",
        start_owner="product-composition",
        stop_owner="product-composition",
    ),
    _session_capability(
        "permission-sandbox",
        "mote.runtime.sandbox.runtime.SandboxRuntime",
        "mote.runtime.agent.components.integrations._build_sandbox_runtime",
        enablement=Enablement.CONFIGURED,
    ),
    _session_capability(
        "background-task",
        "mote.orchestration.background_tasks.pool.BackgroundTaskPool",
        "mote.product.agents.background_tasks.build_background_task_pool",
        required_ports=("mote.contracts.ports.task.operations.BackgroundTaskService",),
    ),
    _session_capability(
        "lsp",
        "mote.runtime.lsp.service.LspService",
        "mote.runtime.agent.components.integrations._build_lsp_service",
        enablement=Enablement.CONFIGURED,
        required_ports=("mote.contracts.ports.code_intelligence.lsp.DiagnosticsProvider",),
    ),
    _session_capability(
        "file-watch",
        "mote.runtime.watching.FileWatchService",
        "mote.runtime.agent.components.watching._build_file_watch_service",
        enablement=Enablement.CONFIGURED,
        required_ports=("mote.contracts.ports.file.changes.FileChangePort",),
    ),
    _session_capability(
        "hook",
        "mote.runtime.hook.manager.HookManager",
        "mote.runtime.agent.components.integrations._build_hook_manager",
        enablement=Enablement.CONFIGURED,
    ),
    CapabilityDeclaration(
        capability_id="hosted-service-gateway",
        implementation="mote.runtime.service_gateway.gateway.RuntimeServiceGateway",
        implementation_owner="runtime",
        applicable_root=PRODUCT_APPLICATION_ROOT,
        enablement=Enablement.REQUIRED,
        canonical_factory="mote.product.composition.service_gateway.builtin_service_gateway",
        required_ports=("mote.contracts.ports.service.gateway.ServiceGateway",),
        deployment_mode=DeploymentMode.EMBEDDED,
        instance_scope=InstanceScope.APPLICATION,
        lifecycle_owner="product-composition",
        start_owner="product-composition",
        stop_owner="product-composition",
    ),
    CapabilityDeclaration(
        capability_id="agent-catalog-factory",
        implementation="mote.product.agents.catalog.AgentCatalog",
        implementation_owner="product",
        applicable_root=PRODUCT_APPLICATION_ROOT,
        enablement=Enablement.REQUIRED,
        canonical_factory="mote.product.composition.container.ProductContainer.standard",
        required_ports=(),
        deployment_mode=DeploymentMode.EMBEDDED,
        instance_scope=InstanceScope.APPLICATION,
        lifecycle_owner="product-composition",
        start_owner="product-composition",
        stop_owner="product-composition",
    ),
    CapabilityDeclaration(
        capability_id="inference-daemon",
        implementation=DAEMON_ROOT,
        implementation_owner="inference-daemon",
        applicable_root=DAEMON_ROOT,
        enablement=Enablement.REQUIRED,
        canonical_factory=DAEMON_ROOT,
        required_ports=("mote.contracts.ports.inference",),
        deployment_mode=DeploymentMode.SHARED_DAEMON,
        instance_scope=InstanceScope.PROCESS,
        lifecycle_owner="inference-daemon",
        start_owner="inference-daemon",
        stop_owner="inference-daemon",
    ),
    *(
        CapabilityDeclaration(
            capability_id=f"optional-interface-{interface}",
            implementation=root,
            implementation_owner=f"{interface}-interface",
            applicable_root=root,
            enablement=Enablement.OPTIONAL_DEPENDENCY,
            canonical_factory=root,
            required_ports=(
                "mote.contracts.ports.interaction",
                "mote.contracts.ports.surface",
            ),
            deployment_mode=DeploymentMode.OPTIONAL_HOST,
            instance_scope=InstanceScope.PROCESS,
            lifecycle_owner=f"{interface}-interface",
            start_owner=f"{interface}-interface",
            stop_owner=f"{interface}-interface",
        )
        for interface, root in (
            ("textual", TEXTUAL_ROOT),
            ("agui", AGUI_ROOT),
            ("acp", ACP_ROOT),
        )
    ),
)

# Reviewed classifications for candidates discovered from canonical factory
# references. The checker derives the candidate source set from Python AST and
# compares it to both this classification and the capability declarations.
CANDIDATE_CLASSIFICATIONS = (
    CandidateClassification(
        "model-runtime",
        "mote.runtime.models.composition.SharedRuntimeCompositionHandle",
        CandidateRole.PRODUCTION_ROOT_REFERENCE,
        "mote.product.composition.model_builder.build_application_candidate",
    ),
    CandidateClassification(
        "telemetry",
        "mote.runtime.events.telemetry.TelemetryRuntime",
        CandidateRole.INFRASTRUCTURE_FACTORY,
        "mote.runtime.agent.role_components._build_telemetry",
    ),
    CandidateClassification(
        "event-fabric",
        "mote.runtime.events.fabric.EventFabric",
        CandidateRole.INFRASTRUCTURE_FACTORY,
        "mote.runtime.agent.components.session._build_event_fabric",
    ),
    CandidateClassification(
        "session-persistence",
        "mote.runtime.session.log.SessionLog",
        CandidateRole.INFRASTRUCTURE_FACTORY,
        "mote.runtime.agent.components.session._build_session_log",
    ),
    CandidateClassification(
        "artifact-store",
        "mote.runtime.artifacts.DurableArtifactStore",
        CandidateRole.GOVERNED_PORT_IMPLEMENTATION,
        "mote.runtime.agent.components.session._build_artifact_store",
    ),
    CandidateClassification(
        "journal",
        "mote.runtime.events.journal.LocalEventJournal",
        CandidateRole.GOVERNED_PORT_IMPLEMENTATION,
        "mote.runtime.session.log.SessionLog",
    ),
    CandidateClassification(
        "tool-executor",
        "mote.runtime.tools.tool_executor.ToolExecutor",
        CandidateRole.INFRASTRUCTURE_FACTORY,
        "mote.runtime.agent.components.action._build_executor",
    ),
    CandidateClassification(
        "tool-catalog",
        "mote.runtime.tools.tool_registry.ToolCatalog",
        CandidateRole.PRODUCTION_ROOT_REFERENCE,
        "mote.product.toolsets.builtin_tool_catalog",
    ),
    CandidateClassification(
        "permission-sandbox",
        "mote.runtime.sandbox.runtime.SandboxRuntime",
        CandidateRole.INFRASTRUCTURE_FACTORY,
        "mote.runtime.agent.components.integrations._build_sandbox_runtime",
    ),
    CandidateClassification(
        "background-task",
        "mote.orchestration.background_tasks.pool.BackgroundTaskPool",
        CandidateRole.GOVERNED_PORT_IMPLEMENTATION,
        "mote.product.agents.background_tasks.build_background_task_pool",
    ),
    CandidateClassification(
        "lsp",
        "mote.runtime.lsp.service.LspService",
        CandidateRole.GOVERNED_PORT_IMPLEMENTATION,
        "mote.runtime.agent.components.integrations._build_lsp_service",
    ),
    CandidateClassification(
        "file-watch",
        "mote.runtime.watching.FileWatchService",
        CandidateRole.GOVERNED_PORT_IMPLEMENTATION,
        "mote.runtime.agent.components.watching._build_file_watch_service",
    ),
    CandidateClassification(
        "hook",
        "mote.runtime.hook.manager.HookManager",
        CandidateRole.INFRASTRUCTURE_FACTORY,
        "mote.runtime.agent.components.integrations._build_hook_manager",
    ),
    CandidateClassification(
        "hosted-service-gateway",
        "mote.runtime.service_gateway.gateway.RuntimeServiceGateway",
        CandidateRole.GOVERNED_PORT_IMPLEMENTATION,
        "mote.product.composition.service_gateway.builtin_service_gateway",
    ),
    CandidateClassification(
        "agent-catalog-factory",
        "mote.product.agents.catalog.AgentCatalog",
        CandidateRole.PRODUCTION_ROOT_REFERENCE,
        "mote.product.composition.container.ProductContainer.standard",
    ),
    CandidateClassification(
        "inference-daemon",
        DAEMON_ROOT,
        CandidateRole.PRODUCTION_ROOT_REFERENCE,
        DAEMON_ROOT,
    ),
    CandidateClassification(
        "optional-interface-textual",
        TEXTUAL_ROOT,
        CandidateRole.PRODUCTION_ROOT_REFERENCE,
        TEXTUAL_ROOT,
    ),
    CandidateClassification(
        "optional-interface-agui",
        AGUI_ROOT,
        CandidateRole.PRODUCTION_ROOT_REFERENCE,
        AGUI_ROOT,
    ),
    CandidateClassification(
        "optional-interface-acp",
        ACP_ROOT,
        CandidateRole.PRODUCTION_ROOT_REFERENCE,
        ACP_ROOT,
    ),
)

# Closed inventory of package-level Runtime/Orchestration exports that are
# themselves governed construction or lifecycle entry points. Data models,
# pure algorithms and ordinary service methods are outside this dangerous-set
# classifier.
PUBLIC_SYMBOL_CLASSIFICATIONS = (
    PublicSymbolClassification(
        "mote.runtime.EngineServices",
        PublicSymbolRole.INTERNAL_FACTORY,
        "runtime",
        "Runtime assembly aggregate; Product roots consume narrower component builders",
    ),
    PublicSymbolClassification(
        "mote.runtime.agent.Role",
        PublicSymbolRole.PRODUCTION_CAPABILITY,
        "runtime",
        "canonical session lifecycle implementation",
    ),
    PublicSymbolClassification(
        "mote.runtime.artifacts.DurableArtifactStore",
        PublicSymbolRole.PRODUCTION_CAPABILITY,
        "runtime",
        "artifact-store capability declaration",
    ),
    PublicSymbolClassification(
        "mote.runtime.events.EventFabric",
        PublicSymbolRole.PRODUCTION_CAPABILITY,
        "runtime",
        "event-fabric capability declaration",
    ),
    PublicSymbolClassification(
        "mote.runtime.inference.GatewayGenerationOwner",
        PublicSymbolRole.PRODUCTION_CAPABILITY,
        "runtime",
        "inference daemon generation lifecycle authority",
    ),
    PublicSymbolClassification(
        "mote.runtime.models.routing.build_route_catalog",
        PublicSymbolRole.INTERNAL_FACTORY,
        "runtime",
        "pure immutable route snapshot builder",
    ),
    PublicSymbolClassification(
        "mote.runtime.prompt.build_prompt_policy",
        PublicSymbolRole.INTERNAL_FACTORY,
        "runtime",
        "session-owned policy builder",
    ),
    PublicSymbolClassification(
        "mote.orchestration.agents.AgentControl",
        PublicSymbolRole.PRODUCTION_CAPABILITY,
        "orchestration",
        "agent control lifecycle capability",
    ),
    PublicSymbolClassification(
        "mote.orchestration.automation.cron.CronService",
        PublicSymbolRole.PRODUCTION_CAPABILITY,
        "orchestration",
        "explicit automation service lifecycle",
    ),
    PublicSymbolClassification(
        "mote.orchestration.background_tasks.BackgroundTaskPool",
        PublicSymbolRole.PRODUCTION_CAPABILITY,
        "orchestration",
        "background-task capability declaration",
    ),
    PublicSymbolClassification(
        "mote.orchestration.workflows.WorkflowBuilder",
        PublicSymbolRole.INTERNAL_FACTORY,
        "orchestration",
        "workflow definition builder, not a runtime service locator",
    ),
)


__all__ = [
    "ACP_ROOT",
    "AGUI_ROOT",
    "CANDIDATE_CLASSIFICATIONS",
    "CAPABILITY_DECLARATIONS",
    "CLASSIFIER_VERSION",
    "PRODUCT_APPLICATION_ROOT",
    "DAEMON_ROOT",
    "FACADE_DECLARATIONS",
    "OWNER_DECLARATIONS",
    "PUBLIC_SYMBOL_CLASSIFICATIONS",
    "TEXTUAL_ROOT",
    "WIRE_AUTHORITY_DECLARATIONS",
]
