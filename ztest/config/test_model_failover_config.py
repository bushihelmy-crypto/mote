from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.config.llm import LLMConfig
from mote.contracts.config.models import ModelsConfig
from mote.runtime.config.diagnostics import unknown_key_paths
from mote.runtime.config.schema import Config


def _base() -> dict:
    return {
        "default": LLMConfig(api_key="sk-test", model="legacy"),
        "credential_pools": {
            "pool": {
                "slots": [
                    {"id": "slot-a", "secret_ref": "env://PRIMARY_KEY"},
                    {"id": "slot-b", "secret_ref": "env://BACKUP_KEY"},
                ]
            }
        },
        "endpoints": {
            "primary": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-8",
                "credential_pool": "pool",
            },
            "backup": {
                "api_key": "sk-backup",
                "model": "gpt-5.4",
            },
        },
        "failover_groups": {
            "interactive": {
                "endpoints": ["primary", "backup"],
                "recovery_profile": "bounded",
            }
        },
        "routes": {"default": "interactive"},
        "recovery_profiles": {
            "bounded": {
                "max_wire_attempts": 2,
                "max_attempts_per_endpoint": 1,
                "max_endpoint_switches": 1,
                "max_credential_rotations": 1,
                "max_request_transforms": 1,
                "total_deadline_seconds": 30,
                "single_attempt_timeout_seconds": 20,
                "max_backoff_seconds": 2,
            }
        },
    }


def test_failover_config_validates_closed_reference_graph() -> None:
    models = ModelsConfig(**_base())

    assert "default" in models.recovery_profiles
    assert models.routes.default == "interactive"
    assert models.failover_groups["interactive"].endpoints == [
        "primary",
        "backup",
    ]


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("endpoints", "primary", "credential_pool"), "missing", "credential pool"),
        (
            ("failover_groups", "interactive", "endpoints"),
            ["primary", "missing"],
            "unknown endpoints",
        ),
        (("routes", "default"), "missing", "unknown failover group"),
        (
            ("failover_groups", "interactive", "recovery_profile"),
            "missing",
            "recovery profile",
        ),
    ],
)
def test_failover_config_rejects_unknown_references(path, value, match) -> None:
    data = _base()
    target = data
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value

    with pytest.raises(ValidationError, match=match):
        ModelsConfig(**data)


def test_compression_profile_forbids_recursive_request_transforms() -> None:
    data = _base()
    data["routes"]["tasks"] = {"compression": "interactive"}

    with pytest.raises(ValidationError, match="compression route"):
        ModelsConfig(**data)


def test_strict_diagnostics_descends_into_named_config_maps() -> None:
    data = _base()
    data["endpoints"]["primary"]["typo_field"] = True
    raw = {"models": data}

    assert unknown_key_paths(raw, Config) == ["models.endpoints.primary.typo_field"]
