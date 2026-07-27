from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mote.contracts.config.llm import LLMConfig
from mote.contracts.config.models import ModelsConfig
from mote.contracts.config.oauth import OAuthProviderConfig
from mote.contracts.errors.models import (
    ModelCapabilityUnsatisfiedError,
    ModelGovernanceViolationError,
    ModelRouteUnavailableError,
)
from mote.contracts.models import (
    CanonicalMessage,
    GenerateInput,
    ModelInvocation,
    ModelOperation,
    RequestRequirements,
    ResponseMode,
)
from mote.runtime.models.failover import FailoverPlanner, build_model_runtime_snapshot

_NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _models(*, primary_key: str = "primary-secret") -> ModelsConfig:
    return ModelsConfig(
        default=LLMConfig(api_key="legacy-secret", model="legacy"),
        endpoints={
            "primary": {
                "api_key": primary_key,
                "model": "plain-model",
                "governance_domain": "corp",
                "region": "us",
                "capabilities": {"context_tokens": 100_000},
            },
            "backup": {
                "api_key": "backup-secret",
                "model": "gpt-5.4",
                "governance_domain": "corp",
                "region": "eu",
                "capabilities": {
                    "supports_tools": True,
                    "context_tokens": 200_000,
                },
            },
            "external": {
                "api_key": "external-secret",
                "model": "gpt-5.4",
                "governance_domain": "external",
                "region": "eu",
                "capabilities": {
                    "supports_tools": True,
                    "context_tokens": 200_000,
                },
            },
        },
        failover_groups={
            "interactive": {
                "endpoints": ["primary", "backup", "external"],
                "recovery_profile": "interactive",
            }
        },
        routes={"default": "interactive"},
        recovery_profiles={
            "interactive": {
                "max_wire_attempts": 3,
                "max_attempts_per_endpoint": 2,
                "max_endpoint_switches": 2,
                "max_credential_rotations": 2,
                "max_request_transforms": 2,
                "total_deadline_seconds": 30,
                "single_attempt_timeout_seconds": 20,
                "max_backoff_seconds": 2,
            }
        },
    )


def _invocation(
    *,
    call_id: str = "call-1",
    route_id: str = "default",
    requirements: RequestRequirements | None = None,
) -> ModelInvocation:
    return ModelInvocation(
        model_call_id=call_id,
        route_id=route_id,
        task="interactive",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(messages=(CanonicalMessage(role="user", content="hello"),)),
        requirements=requirements or RequestRequirements(governance_domain="corp"),
    )


def _planner(models: ModelsConfig | None = None) -> FailoverPlanner:
    return FailoverPlanner(
        build_model_runtime_snapshot(models or _models()),
        clock=lambda: _NOW,
    )


def test_planner_hard_filters_capability_and_governance_in_order() -> None:
    plan = _planner().plan(
        _invocation(
            requirements=RequestRequirements(
                response_mode=ResponseMode.NATIVE_TOOLS,
                needs_tools=True,
                governance_domain="corp",
                allowed_regions=frozenset({"eu", "us"}),
                min_context_tokens=150_000,
            )
        )
    )

    assert [endpoint.endpoint_id for endpoint in plan.endpoints] == ["backup"]
    assert plan.created_at == _NOW
    assert plan.config_revision == _planner().snapshot.revision
    assert plan.budget.max_wire_attempts == 3


def test_each_call_gets_an_immutable_plan_without_cursor_state() -> None:
    planner = _planner()
    first = planner.plan(_invocation(call_id="call-a"))
    second = planner.plan(_invocation(call_id="call-b"))

    assert [endpoint.endpoint_id for endpoint in first.endpoints] == [
        "primary",
        "backup",
    ]
    assert first.endpoints == second.endpoints
    assert first.plan_id != second.plan_id
    with pytest.raises(ValidationError):
        first.endpoints[0].endpoint_id = "changed"


def test_native_tool_search_requirement_filters_non_deferred_endpoints() -> None:
    models = _models()
    models.endpoints["backup"].capabilities.supports_native_tool_search = True

    plan = _planner(models).plan(
        _invocation(
            requirements=RequestRequirements(
                governance_domain="corp",
                needs_native_tool_search=True,
            )
        )
    )

    assert [endpoint.endpoint_id for endpoint in plan.endpoints] == ["backup"]


def test_unknown_route_fails_before_any_attempt() -> None:
    with pytest.raises(ModelRouteUnavailableError) as raised:
        _planner().plan(_invocation(route_id="missing"))

    assert raised.value.context["route_id"] == "missing"


def test_governance_rejection_is_distinct_from_capability_rejection() -> None:
    with pytest.raises(ModelGovernanceViolationError):
        _planner().plan(
            _invocation(
                requirements=RequestRequirements(
                    governance_domain="secret-domain",
                )
            )
        )

    with pytest.raises(ModelCapabilityUnsatisfiedError) as raised:
        _planner().plan(
            _invocation(
                requirements=RequestRequirements(
                    governance_domain="corp",
                    needs_pdf=True,
                )
            )
        )
    assert raised.value.context["missing_by_endpoint"] == {
        "primary": ["pdf"],
        "backup": ["pdf"],
    }


def test_snapshot_revision_and_repr_are_secret_opaque() -> None:
    first = build_model_runtime_snapshot(_models(primary_key="secret-a"))
    second = build_model_runtime_snapshot(_models(primary_key="secret-b"))
    changed = _models(primary_key="secret-a")
    changed.endpoints["primary"].model = "another-model"
    third = build_model_runtime_snapshot(changed)

    assert first.revision == second.revision
    assert first.revision != third.revision
    assert "secret-a" not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.revision = "changed"


def test_legacy_default_and_tasks_compile_to_singleton_groups() -> None:
    models = ModelsConfig(
        default=LLMConfig(api_key="secret", model="gpt-4o"),
        tasks={"compression": LLMConfig(api_key="secret", model="gpt-4o")},
    )
    snapshot = build_model_runtime_snapshot(models)
    planner = FailoverPlanner(snapshot, clock=lambda: _NOW)

    default = planner.plan(
        _invocation(
            requirements=RequestRequirements(
                response_mode=ResponseMode.NATIVE_TOOLS,
                needs_tools=True,
            )
        )
    )
    compression = planner.plan(_invocation(route_id="compression", requirements=RequestRequirements()))

    assert [endpoint.endpoint_id for endpoint in default.endpoints] == ["default"]
    assert [endpoint.endpoint_id for endpoint in compression.endpoints] == ["compression"]


def test_oauth_compiles_to_current_and_refresh_slots() -> None:
    models = ModelsConfig(
        default=LLMConfig(
            api_key="",
            model="oauth-model",
            oauth=OAuthProviderConfig(
                token_url="https://issuer.example.test/token",
                client_id="client",
            ),
        ),
        tasks={},
    )

    snapshot = build_model_runtime_snapshot(models)

    assert snapshot.slots_for_endpoint("default") == (
        "default:oauth-current",
        "default:oauth-refresh",
    )
