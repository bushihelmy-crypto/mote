"""Optional hook, LSP, sandbox, and secrets integration manifest."""

from __future__ import annotations

from pathlib import Path

from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.component_keys import (
    DIAGNOSTICS_BUFFER,
    HOOK_MANAGER,
    LSP_SERVICE,
    SANDBOX_RUNTIME,
    SECRET_STORE,
    TELEMETRY,
)
from mote.runtime.hook import HookManager
from mote.runtime.hook.subscriber import HookSubscriber
from mote.runtime.secrets.cipher import build_cipher
from mote.runtime.secrets.store import SecretStore, secrets_config_path, secrets_path
from mote.runtime.tools.permission.config import SandboxConfig
from mote.runtime.tools.permission.sandbox.adapter import build_runtime
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard
from mote.runtime.tools.permission.sandbox.resource_guard import ResourceGuard


def integration_component_specs() -> list[ComponentSpec]:
    return [
        ComponentSpec(HOOK_MANAGER, _build_hook_manager, available=hook_available),
        ComponentSpec(LSP_SERVICE, _build_lsp_service, available=_lsp_available),
        ComponentSpec(
            DIAGNOSTICS_BUFFER,
            _build_diagnostics_provider,
            available=_lsp_available,
        ),
        ComponentSpec(SANDBOX_RUNTIME, _build_sandbox_runtime, available=_sandbox_available),
        ComponentSpec(SECRET_STORE, _build_secret_store),
    ]


def integration_event_subscribers(get_hook_manager) -> list:
    """Return subscribers owned by enabled optional integrations."""
    hook_manager = get_hook_manager()
    return [
        HookSubscriber(hook_manager) if hook_manager is not None else None,
    ]


def hook_available(role, state) -> bool:
    if role.role_schema.hooks is not None or bool(state.hook_callbacks):
        return True
    return role.wiring.dependencies.hook_config is not None


def _build_hook_manager(ctx):
    role = ctx.role
    configs = tuple(
        config for config in (role.wiring.dependencies.hook_config, role.role_schema.hooks) if config is not None
    )
    merged = None
    if configs:
        events: dict[str, list] = {}
        for config in configs:
            for event, groups in config.events.items():
                events.setdefault(event, []).extend(groups)
        merged = type(configs[0])(events=events)
    manager = HookManager(merged, session_id=role.state.session_id, get_cwd=role.get_cwd)
    for event, fn, matcher in ctx.state.hook_callbacks:
        manager.register(event, fn, matcher)
    return manager


def _lsp_available(role, state) -> bool:
    cfg = role.role_schema.lsp
    return (
        role.wiring.dependencies.lsp_service_factory is not None
        and cfg is not None
        and cfg.enabled
        and bool(cfg.servers)
    )


def _build_lsp_service(ctx):
    cfg = ctx.role.role_schema.lsp
    root = ctx.role.state.project_root or ctx.role.get_cwd()
    return ctx.role.wiring.dependencies.lsp_service_factory.build_service(cfg, Path(root), ctx.dep(TELEMETRY))


def _build_diagnostics_provider(ctx):
    return ctx.role.wiring.dependencies.lsp_service_factory.build_diagnostics_provider()


def _sandbox_available(role, state) -> bool:
    permissions = role.role_schema.permissions
    cfg = permissions.runtime if permissions is not None else None
    return cfg is not None and cfg.enabled


def _build_secret_store(ctx):
    secrets_cfg = ctx.role.config.secrets
    secrets_root = ctx.role.wiring.dependencies.secrets_root
    if secrets_root is None:
        raise ValueError("Agent composition requires a secrets root")
    cipher = build_cipher(secrets_cfg, default_key_path=secrets_root / "vault.key")
    vault_path = Path(secrets_cfg.vault_path) if secrets_cfg.vault_path else secrets_path(secrets_root)
    secrets_file = (
        Path(secrets_cfg.secrets_config_path) if secrets_cfg.secrets_config_path else secrets_config_path(secrets_root)
    )
    return SecretStore(
        cipher,
        vault_path=vault_path,
        config_path=ctx.role.wiring.dependencies.primary_config_path,
        secrets_config_file=secrets_file,
        is_secret=ctx.role.wiring.dependencies.config_secret_predicate,
    )


def _build_sandbox_runtime(ctx):
    permissions = ctx.role.role_schema.permissions
    cfg = permissions.runtime
    role = ctx.role
    sandbox_cfg = (permissions.sandbox if permissions is not None else None) or SandboxConfig()

    def guard_factory() -> SandboxGuard:
        return SandboxGuard(sandbox_cfg, get_cwd=role.get_cwd)

    store = ctx.dep(SECRET_STORE)
    secret_lookup = store.get if store is not None else None
    ctx.state.resource_guard = ResourceGuard(cfg)
    dependencies = role.wiring.dependencies
    if (
        dependencies.secrets_root is None
        or dependencies.browser_profiles_root is None
        or dependencies.sandbox_ca_root is None
    ):
        raise ValueError("Sandbox composition requires explicit persistent roots")
    return build_runtime(
        cfg,
        get_cwd=role.get_cwd,
        guard_factory=guard_factory,
        resource_guard=ctx.state.resource_guard,
        secret_lookup=secret_lookup,
        secrets_root=dependencies.secrets_root,
        browser_profiles_root=dependencies.browser_profiles_root,
        sandbox_ca_root=dependencies.sandbox_ca_root,
    )


__all__ = [
    "hook_available",
    "integration_component_specs",
    "integration_event_subscribers",
]
