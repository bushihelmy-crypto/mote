"""Ownership contracts for domain component manifests."""

from mote.roles.runtime_modules import (
    WatchingCallbacks,
    action_component_specs,
    cognition_component_specs,
    context_component_specs,
    integration_component_specs,
    session_component_specs,
    watching_component_specs,
)


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


def test_session_module_owns_complete_component_keyset():
    assert {spec.name for spec in session_component_specs()} == {
        "session_log",
        "file_snapshot_recorder",
        "hunk_ledger",
        "hunk_subscriber",
        "checkpoint_subscriber",
        "title_subscriber",
        "terminal_state_recorder",
        "kernel_state_recorder",
        "browser_state_recorder",
    }


def test_session_manifest_has_no_duplicate_keys():
    names = [spec.name for spec in session_component_specs()]
    assert len(names) == len(set(names))


def test_integrations_module_owns_complete_component_keyset():
    assert {spec.name for spec in integration_component_specs()} == {
        "hook_manager",
        "lsp_service",
        "diagnostics_buffer",
        "sandbox_runtime",
        "secret_store",
    }


def test_domain_manifests_do_not_claim_the_same_component():
    names = [
        spec.name
        for spec in [
            *action_component_specs(),
            *cognition_component_specs(),
            *context_component_specs(),
            *session_component_specs(),
            *integration_component_specs(),
            *_watching_specs(),
        ]
    ]
    assert len(names) == len(set(names))


def test_action_module_owns_complete_component_keyset():
    assert {spec.name for spec in action_component_specs()} == {
        "workspace_store",
        "bg_pool",
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
    assert {spec.name for spec in cognition_component_specs()} == {
        "router",
        "context_provider",
        "think_engine_factory",
        "think_subsystems_factory",
        "loop_factory",
    }


def test_watching_module_owns_complete_component_keyset():
    assert {spec.name for spec in _watching_specs()} == {"file_watch_service"}
