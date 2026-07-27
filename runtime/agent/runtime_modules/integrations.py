"""Optional hook, LSP, sandbox, and secrets integration manifest."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from mote.contracts.settings.permissions import SandboxConfig
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.lsp import DiagnosticsBuffer, LspService
from mote.runtime.config.sources import discover_source_files
from mote.runtime.hook import HookManager, load_global_hooks, merge_hook_configs
from mote.runtime.hook.subscriber import HookSubscriber
from mote.runtime.secrets.cipher import build_cipher
from mote.runtime.secrets.store import SecretStore, secrets_config_path, secrets_path
from mote.runtime.tools.permission.sandbox.adapter import build_runtime
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard
from mote.runtime.tools.permission.sandbox.resource_guard import ResourceGuard


def integration_component_specs() -> list[ComponentSpec]:
    return [
        ComponentSpec("hook_manager", _build_hook_manager, available=hook_available),
        ComponentSpec("lsp_service", _build_lsp_service, available=_lsp_available),
        ComponentSpec(
            "diagnostics_buffer",
            lambda ctx: DiagnosticsBuffer(),
            available=_lsp_available,
        ),
        ComponentSpec("sandbox_runtime", _build_sandbox_runtime, available=_sandbox_available),
        ComponentSpec("secret_store", _build_secret_store),
    ]


def integration_event_subscribers(get) -> list:
    """Return subscribers owned by enabled optional integrations."""
    hook_manager = get("hook_manager")
    return [
        HookSubscriber(hook_manager) if hook_manager is not None else None,
        get("lsp_service"),
    ]


def hook_available(role, state) -> bool:
    if role.role_schema.hooks is not None or bool(state.hook_callbacks):
        return True
    return load_global_hooks(role.get_cwd()) is not None


def _build_hook_manager(ctx):
    role = ctx.role
    merged = merge_hook_configs(load_global_hooks(role.get_cwd()), role.role_schema.hooks)
    manager = HookManager(merged, session_id=role.state.session_id, get_cwd=role.get_cwd)
    for event, fn, matcher in ctx.state.hook_callbacks:
        manager.register(event, fn, matcher)
    return manager


def _lsp_available(role, state) -> bool:
    cfg = role.role_schema.lsp
    return cfg is not None and cfg.enabled and bool(cfg.servers)


def _build_lsp_service(ctx):
    cfg = ctx.role.role_schema.lsp
    root = ctx.role.state.project_root or ctx.role.get_cwd()
    return LspService(cfg, root)


def _sandbox_available(role, state) -> bool:
    permissions = role.role_schema.permissions
    cfg = permissions.runtime if permissions is not None else None
    return cfg is not None and cfg.enabled


def _primary_config_path(cwd) -> Optional[Path]:
    try:
        files = discover_source_files(Path(cwd))
    except Exception:  # noqa: BLE001 — discovery is best-effort
        return None
    return files[-1].path if files else None


def _build_secret_store(ctx):
    secrets_cfg = ctx.role.config.secrets
    cipher = build_cipher(secrets_cfg)
    vault_path = Path(secrets_cfg.vault_path) if secrets_cfg.vault_path else secrets_path()
    secrets_file = Path(secrets_cfg.secrets_config_path) if secrets_cfg.secrets_config_path else secrets_config_path()
    return SecretStore(
        cipher,
        vault_path=vault_path,
        config_path=_primary_config_path(ctx.role.get_cwd()),
        secrets_config_file=secrets_file,
    )


def _build_sandbox_runtime(ctx):
    permissions = ctx.role.role_schema.permissions
    cfg = permissions.runtime
    role = ctx.role
    sandbox_cfg = (permissions.sandbox if permissions is not None else None) or SandboxConfig()

    def guard_factory() -> SandboxGuard:
        return SandboxGuard(sandbox_cfg, get_cwd=role.get_cwd)

    store = ctx.dep("secret_store")
    secret_lookup = store.get if store is not None else None
    ctx.state.resource_guard = ResourceGuard(cfg)
    return build_runtime(
        cfg,
        get_cwd=role.get_cwd,
        guard_factory=guard_factory,
        resource_guard=ctx.state.resource_guard,
        secret_lookup=secret_lookup,
    )


__all__ = [
    "hook_available",
    "integration_component_specs",
    "integration_event_subscribers",
]
