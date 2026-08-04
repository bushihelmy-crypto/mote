"""Context-domain component manifest for a Role runtime."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.conversation.fields import TOOL_CALLS
from mote.contracts.events.conversation import ModelContextRebuiltEvent
from mote.contracts.output import OutputBindingKind
from mote.contracts.ports.code_intelligence.code_map import CodeMapIndexerFactory, CodeMapTurnSourceRequest
from mote.contracts.ports.conversation.compaction_policy import CompactionPolicyExtensionSpec
from mote.contracts.ports.conversation.turn_context import EphemeralContextSource
from mote.contracts.ports.skill.registry import SkillService, SkillServiceFactory
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.component_keys import (
    COMMAND_CHANNEL,
    CONTEXT_MANAGER,
    CONTEXT_VISIBILITY,
    DIAGNOSTICS_BUFFER,
    EXECUTOR,
    FILE_OPERATIONS,
    HOOK_MANAGER,
    LSP_SERVICE,
    REPO_INDEX,
    RESOURCE_REGISTRY,
    ROUTER,
    SECRET_STORE,
    SESSION_FACT_COMMITTER,
    SESSION_MANAGER,
    SKILL_MANAGER,
    TELEMETRY,
    TURN_CONTEXT_BUS,
    TURN_CONTEXT_SOURCES,
    WORKSPACE_STORE,
)
from mote.runtime.agent.components.action import effective_deferred_tools
from mote.runtime.agent.components.output_context import OutputContractContextSource
from mote.runtime.agent.control import resolve_control
from mote.runtime.context import ContextManager, ContextVisibility
from mote.runtime.context.compaction import FileRehydrator
from mote.runtime.context.compaction.policy import build_compaction_policy
from mote.runtime.context.turn import (
    ChangedFilesContextSource,
    CompactionNoticeContextSource,
    CredentialIndexContextSource,
    DeferredToolIndexContextSource,
    FoldPressureContextSource,
    GitContextSource,
    SkillActivationContextSource,
    SkillListingContextSource,
    SplitToolMenuContextSource,
    TeamContextSource,
    TimestampContextSource,
    TokenPressureContextSource,
    ToolCatalogContextSource,
    ToolsetInstructionsContextSource,
    TurnContextBus,
)
from mote.runtime.lsp.server import LspQueryError
from mote.runtime.models.gateway import COMPRESSION_TASK
from mote.runtime.resources import ResourceRegistry
from mote.runtime.vcs import find_git_root


@dataclass(frozen=True, slots=True)
class ContextComponentInputs:
    skill_service_factory: SkillServiceFactory | None = None
    code_map_indexer_factory: CodeMapIndexerFactory | None = None
    compaction_policy_extensions: tuple[CompactionPolicyExtensionSpec, ...] = ()


def context_component_specs(inputs: ContextComponentInputs = ContextComponentInputs()) -> list[ComponentSpec]:
    """Return the complete, uniquely owned context-domain graph fragment."""
    return [
        ComponentSpec(SKILL_MANAGER, lambda ctx: _build_skill_manager(ctx, inputs)),
        ComponentSpec(RESOURCE_REGISTRY, lambda ctx: ResourceRegistry()),
        ComponentSpec(CONTEXT_MANAGER, lambda ctx: _build_context_manager(ctx, inputs)),
        ComponentSpec(CONTEXT_VISIBILITY, _build_context_visibility),
        ComponentSpec(REPO_INDEX, lambda ctx: _build_repo_index(ctx, inputs), available=_repo_index_available),
        ComponentSpec(TURN_CONTEXT_SOURCES, lambda ctx: _build_turn_context_sources(ctx, inputs)),
        ComponentSpec(
            TURN_CONTEXT_BUS,
            lambda ctx: TurnContextBus(ctx.dep(TURN_CONTEXT_SOURCES)),
        ),
    ]


class _DisabledSkillService:
    ready = True
    enabled = False
    pool = None
    injector = None

    def ensure_ready(self) -> None:
        return None

    def reload(self) -> bool:
        return False

    def source_dirs(self) -> list[str]:
        return []


def _build_skill_manager(ctx, inputs: ContextComponentInputs) -> SkillService:
    cfg = ctx.role.config.context.skills
    factory = inputs.skill_service_factory
    if factory is None:
        return _DisabledSkillService()
    return factory.build(
        skills=ctx.role.role_schema.skills,
        config=cfg,
        cwd=ctx.role.get_cwd(),
    )


def _repo_index_root(role) -> str | None:
    return find_git_root(role.state.project_root) or find_git_root(role.get_cwd())


def _repo_index_available(role, state) -> bool:
    return _repo_index_root(role) is not None


def _build_repo_index(ctx, inputs: ContextComponentInputs):
    root = _repo_index_root(ctx.role)
    factory = inputs.code_map_indexer_factory
    return factory.build(root) if root is not None and factory is not None else None


def _touched_files(file_operations) -> list[str]:
    return list(file_operations.observed_versions().keys())


def _glimpsed_files(state) -> list[str]:
    return list(state._file_glimpsed_state.keys())


def _read_state(file_operations) -> dict:
    return file_operations.observed_versions()


def _build_context_manager(ctx, inputs: ContextComponentInputs) -> ContextManager:
    role = ctx.role
    get_file_operations = ctx.defer(FILE_OPERATIONS)
    executor = ctx.dep(EXECUTOR)
    registry = ctx.dep(RESOURCE_REGISTRY)
    get_session_manager = ctx.defer(SESSION_MANAGER)
    get_turn_context_bus = ctx.defer(TURN_CONTEXT_BUS)
    rehydrator = FileRehydrator(lambda: _touched_files(get_file_operations()))

    async def model_context_rebuilt(event: ModelContextRebuiltEvent) -> None:
        await get_turn_context_bus().model_context_rebuilt(event)

    compression_route = ctx.dep(ROUTER).model_route_for_task(COMPRESSION_TASK)
    return ContextManager(
        role.state.context,
        model_route=compression_route,
        model=compression_route.profile.model,
        context_tokens=compression_route.profile.capabilities.context_tokens,
        telemetry=ctx.dep(TELEMETRY),
        sticky_provider=registry.project,
        rehydrate_provider=rehydrator.project,
        compactable=executor.reconstructable_tool_names(),
        compactable_provider=lambda: frozenset(executor.reconstructable_tool_names()),
        write_fold_names=executor.tool_alias_names("Edit"),
        session_id=role.state.session_id,
        store=ctx.dep(WORKSPACE_STORE),
        limit_config=executor.limit_config,
        compaction_policy=build_compaction_policy(
            hook_manager=ctx.dep(HOOK_MANAGER),
            extensions=inputs.compaction_policy_extensions,
        ),
        session_fact_sink=ctx.dep(SESSION_FACT_COMMITTER),
        history_edited=lambda event: get_session_manager().reconcile_resources(event.remaining_messages),
        model_context_rebuilt=model_context_rebuilt,
    )


def _build_context_visibility(ctx) -> ContextVisibility:
    conversation = ctx.role.state.context
    return ContextVisibility(lambda: conversation.messages)


class _LspCodeQuery:
    """None-safe facade that resolves the optional LSP service per call."""

    def __init__(self, get_lsp) -> None:
        self._get_lsp = get_lsp

    async def document_symbols(self, path: str) -> list:
        service = self._get_lsp()
        if service is None:
            raise LspQueryError("LSP capability is not active")
        return await service.document_symbols(path)

    async def definition(self, path: str, line: int, character: int) -> list:
        service = self._get_lsp()
        if service is None:
            raise LspQueryError("LSP capability is not active")
        return await service.definition(path, line, character)

    async def references(self, path: str, line: int, character: int) -> list:
        service = self._get_lsp()
        if service is None:
            raise LspQueryError("LSP capability is not active")
        return await service.references(path, line, character)


def _uses_native_tool_search(role) -> bool:
    if role.role_schema.command_protocol != "native" or not effective_deferred_tools(role):
        return False
    profile = role._components.router.model_route().profile
    return profile.capabilities.supports_native_tool_search


def _credential_labels(store, keys: list[str], inline: dict[str, str]) -> dict[str, str]:
    secret_labels = store.labels() if store else {}
    if keys:
        secret_labels = {key: value for key, value in secret_labels.items() if key in keys}
    merged = {name: value for name, value in inline.items() if value}
    merged.update(secret_labels)
    return merged


def _browser_recently_used(context_manager) -> bool:
    if context_manager is None:
        return False
    for message in context_manager.get(6):
        calls = message.metadata.get(TOOL_CALLS)
        if calls and any(call.get("name") == "WebBrowser" for call in calls):
            return True
    return False


def _credential_index_active(role, store) -> bool:
    return (
        role.config.context.turn_context.credential_index
        and store is not None
        and "WebBrowser" in role.role_schema.tools
    )


def _build_turn_context_sources(ctx, inputs: ContextComponentInputs) -> list[EphemeralContextSource]:
    role = ctx.role
    state = role.state
    get_file_operations = ctx.defer(FILE_OPERATIONS)
    mcp_config = role.config.mcp
    skills_config = role.config.context.skills
    get_executor = ctx.defer(EXECUTOR)
    get_context_manager = ctx.defer(CONTEXT_MANAGER)
    get_skill_manager = ctx.defer(SKILL_MANAGER)
    sources: list[EphemeralContextSource] = [
        ToolCatalogContextSource(
            get_executor=get_executor,
            get_channel=ctx.defer(COMMAND_CHANNEL),
            mcp_enabled=lambda: mcp_config.enabled,
        ),
        ToolsetInstructionsContextSource(get_executor=get_executor),
        GitContextSource(get_cwd=lambda: state.working_dir or None),
        TeamContextSource(
            get_session_id=lambda: state.session_id,
            get_provider=resolve_control,
        ),
        TimestampContextSource(),
        TokenPressureContextSource(get_context_manager),
        FoldPressureContextSource(get_context_manager),
        CompactionNoticeContextSource(),
        SkillActivationContextSource(
            get_pool=lambda: get_skill_manager().pool,
            get_touched_files=lambda: _touched_files(get_file_operations()),
        ),
        SkillListingContextSource(
            get_injector=lambda: get_skill_manager().injector,
            max_tokens=skills_config.max_tokens,
            is_enabled=lambda: skills_config.enabled,
        ),
        ChangedFilesContextSource(),
    ]
    code_map_factory = inputs.code_map_indexer_factory
    if code_map_factory is not None:
        sources.append(
            code_map_factory.build_turn_source(
                CodeMapTurnSourceRequest(
                    get_touched_files=lambda: _touched_files(get_file_operations()),
                    lsp_query=_LspCodeQuery(ctx.defer(LSP_SERVICE)),
                    repo_index=ctx.dep(REPO_INDEX),
                    get_read_state=lambda: _read_state(get_file_operations()),
                    get_glimpsed_files=lambda: _glimpsed_files(state),
                    surface_callers=role.config.context.code_map.surface_callers,
                )
            )
        )
    binding = ctx.dep(COMMAND_CHANNEL).output_binding(is_text=role.output_contract.is_text)
    if binding.kind is OutputBindingKind.PROMPTED_JSON:
        sources.insert(0, OutputContractContextSource(role.output_contract))
    diagnostics = ctx.dep(DIAGNOSTICS_BUFFER)
    if diagnostics is not None:
        sources.append(diagnostics)
    if effective_deferred_tools(role):
        if role.role_schema.command_protocol == "native" and not _uses_native_tool_search(role):
            sources.append(SplitToolMenuContextSource(get_menu=lambda: get_executor().split_tool_menu()))
        else:
            sources.append(
                DeferredToolIndexContextSource(
                    get_index=lambda: get_executor().deferred_tool_index(include_revealed=False)
                )
            )
    disabled = set(role.config.context.turn_context.disabled)
    if disabled:
        sources = [source for source in sources if source.name not in disabled]
    store = ctx.dep(SECRET_STORE)
    if _credential_index_active(role, store):
        turn_context = role.config.context.turn_context
        keys = list(turn_context.credential_keys)
        inline = dict(turn_context.credential_values)
        sources.append(
            CredentialIndexContextSource(
                get_labels=lambda: _credential_labels(store, keys, inline),
                browser_recently_used=lambda: _browser_recently_used(get_context_manager()),
            )
        )
    return sources
