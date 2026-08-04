"""Optional hook, LSP, sandbox, and secrets integration manifest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mote.contracts.agent import ApprovedDeclaration
from mote.contracts.ports.code_intelligence.lsp import LspServiceFactory
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.component_keys import (
    DIAGNOSTICS_BUFFER,
    HOOK_MANAGER,
    LSP_SERVICE,
    PERMISSION_ENGINE,
    SANDBOX_RUNTIME,
    SECRET_STORE,
    TELEMETRY,
)
from mote.runtime.config.hook import HookConfig
from mote.runtime.hook import HookManager
from mote.runtime.hook.subscriber import HookSubscriber
from mote.runtime.secrets.cipher import build_aes_cipher
from mote.runtime.secrets.store import SecretStore, secrets_config_path, secrets_path
from mote.runtime.tools.permission.config import SandboxConfig
from mote.runtime.tools.permission.sandbox.adapter import build_runtime
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard
from mote.runtime.tools.permission.sandbox.resource_guard import ResourceGuard


@dataclass(frozen=True, slots=True)
class IntegrationComponentInputs:
    approved_hooks: ApprovedDeclaration[HookConfig] | None = None
    lsp_service_factory: LspServiceFactory | None = None
    secrets_root: Path | None = None
    browser_profiles_root: Path | None = None
    sandbox_ca_root: Path | None = None
    primary_config_path: Path | None = None
    config_secret_predicate: Callable[[str], bool] | None = None


def integration_component_specs(
    inputs: IntegrationComponentInputs = IntegrationComponentInputs(),
) -> list[ComponentSpec]:
    return [
        ComponentSpec(
            HOOK_MANAGER,
            lambda ctx: _build_hook_manager(ctx, inputs),
            available=lambda role, state: hook_available(role, state, inputs.approved_hooks),
        ),
        ComponentSpec(
            LSP_SERVICE,
            lambda ctx: _build_lsp_service(ctx, inputs),
            available=lambda role, state: _lsp_available(role, state, inputs),
        ),
        ComponentSpec(
            DIAGNOSTICS_BUFFER,
            lambda ctx: _build_diagnostics_provider(ctx, inputs),
            available=lambda role, state: _lsp_available(role, state, inputs),
        ),
        ComponentSpec(SANDBOX_RUNTIME, lambda ctx: _build_sandbox_runtime(ctx, inputs), available=_sandbox_available),
        ComponentSpec(SECRET_STORE, lambda ctx: _build_secret_store(ctx, inputs)),
    ]


def integration_event_subscribers(get_hook_manager) -> list:
    """Return subscribers owned by enabled optional integrations."""
    hook_manager = get_hook_manager()
    return [
        HookSubscriber(hook_manager) if hook_manager is not None else None,
    ]


def hook_available(role, state, approved_hooks=None) -> bool:
    if role.role_schema.hooks is not None or bool(state.hook_callbacks):
        return True
    return approved_hooks is not None


def _build_hook_manager(ctx, inputs: IntegrationComponentInputs):
    role = ctx.role
    configs = tuple(
        config
        for config in (
            inputs.approved_hooks.value if inputs.approved_hooks is not None else None,
            role.role_schema.hooks,
        )
        if config is not None
    )
    merged = None
    if configs:
        events: dict[str, list] = {}
        for config in configs:
            for event, groups in config.events.items():
                events.setdefault(event, []).extend(groups)
        merged = type(configs[0])(events=events)
    manager = HookManager(
        merged,
        session_id=role.state.session_id,
        get_cwd=role.get_cwd,
        command_sandbox=ctx.dep(SANDBOX_RUNTIME),
        permission_engine=ctx.dep(PERMISSION_ENGINE),
    )
    for event, fn, matcher in ctx.state.hook_callbacks:
        manager.register_async(event, fn, matcher)
    return manager


def _lsp_available(role, state, inputs: IntegrationComponentInputs) -> bool:
    cfg = role.role_schema.lsp
    return inputs.lsp_service_factory is not None and cfg is not None and cfg.enabled and bool(cfg.servers)


def _build_lsp_service(ctx, inputs: IntegrationComponentInputs):
    cfg = ctx.role.role_schema.lsp
    root = ctx.role.state.project_root or ctx.role.get_cwd()
    factory = inputs.lsp_service_factory
    if factory is None:
        raise RuntimeError("LSP component built without an LSP service factory")
    return factory.build_service(cfg, Path(root), ctx.dep(TELEMETRY))


def _build_diagnostics_provider(ctx, inputs: IntegrationComponentInputs):
    factory = inputs.lsp_service_factory
    if factory is None:
        raise RuntimeError("diagnostics component built without an LSP service factory")
    return factory.build_diagnostics_provider()


def _sandbox_available(role, state) -> bool:
    permissions = role.role_schema.permissions
    cfg = permissions.runtime if permissions is not None else None
    return cfg is not None


def _build_secret_store(ctx, inputs: IntegrationComponentInputs):
    secrets_cfg = ctx.role.config.secrets
    secrets_root = inputs.secrets_root
    if secrets_root is None:
        raise ValueError("Agent composition requires a secrets root")
    cipher = build_aes_cipher(secrets_root / "vault.key")
    vault_path = Path(secrets_cfg.vault_path) if secrets_cfg.vault_path else secrets_path(secrets_root)
    secrets_file = (
        Path(secrets_cfg.secrets_config_path) if secrets_cfg.secrets_config_path else secrets_config_path(secrets_root)
    )
    return SecretStore(
        cipher,
        vault_path=vault_path,
        config_path=inputs.primary_config_path,
        secrets_config_file=secrets_file,
        is_secret=inputs.config_secret_predicate,
    )


def _build_sandbox_runtime(ctx, inputs: IntegrationComponentInputs):
    permissions = ctx.role.role_schema.permissions
    cfg = permissions.runtime
    role = ctx.role
    sandbox_cfg = (permissions.sandbox if permissions is not None else None) or SandboxConfig(profile=cfg.profile)

    def guard_factory() -> SandboxGuard:
        return SandboxGuard(sandbox_cfg, get_cwd=role.get_cwd)

    store = ctx.dep(SECRET_STORE)
    secret_lookup = store.get if store is not None else None
    ctx.state.resource_guard = ResourceGuard(cfg)
    if inputs.secrets_root is None or inputs.browser_profiles_root is None or inputs.sandbox_ca_root is None:
        raise ValueError("Sandbox composition requires explicit persistent roots")
    return build_runtime(
        cfg,
        get_cwd=role.get_cwd,
        guard_factory=guard_factory,
        resource_guard=ctx.state.resource_guard,
        secret_lookup=secret_lookup,
        secrets_root=inputs.secrets_root,
        browser_profiles_root=inputs.browser_profiles_root,
        sandbox_ca_root=inputs.sandbox_ca_root,
    )


__all__ = [
    "hook_available",
    "IntegrationComponentInputs",
    "integration_component_specs",
    "integration_event_subscribers",
]
