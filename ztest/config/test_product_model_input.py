from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.product.config.model.inputs import ExplicitModelsConfig, ShortcutModelsConfig, parse_product_models_config
from mote.product.config.model.merge import ModelLayer, merge_product_model_layers


def _shortcut() -> dict:
    return {
        "mode": "shortcut",
        "default": {"model": "gpt", "api_key": "secret"},
        "tasks": {"compression": {"model": "small", "api_key": "task-secret"}},
    }


def _explicit() -> dict:
    return {
        "mode": "explicit",
        "endpoints": {
            "primary": {
                "model": "gpt",
                "credential_pool": "pool",
                "capabilities": {"supports_tools": True, "context_tokens": 10},
            }
        },
        "credential_pools": {"pool": {"slots": [{"id": "one", "secret_ref": "env://KEY"}]}},
        "failover_groups": {"main": {"endpoints": ["primary"], "recovery_profile": "default"}},
        "routes": {"default": "main"},
        "recovery_profiles": {"default": {}},
    }


def test_discriminator_selects_input_type() -> None:
    assert isinstance(parse_product_models_config(_shortcut()), ShortcutModelsConfig)
    assert isinstance(parse_product_models_config(_explicit()), ExplicitModelsConfig)


def test_cross_mode_fields_are_rejected() -> None:
    value = _shortcut()
    value["endpoints"] = {}
    with pytest.raises(ValidationError):
        parse_product_models_config(value)


def test_mode_change_replaces_subtree_and_provenance() -> None:
    result = merge_product_model_layers([ModelLayer("base", _shortcut()), ModelLayer("profile", _explicit())])
    assert result.data == _explicit()
    assert "default" not in result.data
    assert all(source == "profile" for source in result.provenance.values())


def test_same_mode_overlays_fields() -> None:
    result = merge_product_model_layers(
        [
            ModelLayer("base", _shortcut()),
            ModelLayer("cli", {"mode": "shortcut", "response_language": "english"}),
        ]
    )
    assert result.data["default"]["model"] == "gpt"
    assert result.data["response_language"] == "english"
    assert result.provenance["models.response_language"] == "cli"


def test_untrusted_replace_cannot_inject_or_inherit_credentials() -> None:
    explicit = _explicit()
    explicit["endpoints"]["primary"]["api_key"] = "canary-endpoint"
    result = merge_product_model_layers(
        [
            ModelLayer("trusted", _shortcut()),
            ModelLayer("workdir", explicit, trusted=False),
        ]
    )
    assert "default" not in result.data
    assert "credential_pools" not in result.data
    assert "api_key" not in result.data["endpoints"]["primary"]
    assert "canary" not in repr(result)


def test_missing_mode_is_rejected() -> None:
    value = _shortcut()
    value.pop("mode")
    with pytest.raises(ValueError, match="models.mode is required"):
        merge_product_model_layers([ModelLayer("user", value)])
    with pytest.raises(ValueError, match="models.mode is required"):
        merge_product_model_layers([ModelLayer("user", {"endpoints": {}})])
