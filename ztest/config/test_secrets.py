"""Governed api_key_helper configuration and activation tests."""

from __future__ import annotations

import sys

import pytest

from mote.product.config.layers import CREDENTIAL_DENYLIST, ConfigLayer, ConfigLayerStack, strip_sensitive
from mote.product.config.loader import load_config
from mote.product.config.model.inputs import ShortcutModelsConfig
from mote.product.config.sources import ConfigSource
from mote.product.models.credential_sources import ProductCredentialSourceCatalog
from mote.product.models.secrets import CredentialWireAccess


def _models(helper=None, *, api_key=None):
    value = {
        "mode": "shortcut",
        "default": {"model": "claude-sonnet-4-8", "api_key": api_key},
    }
    if helper is not None:
        value["api_key_helper"] = helper
    return value


def _helper_catalog(tmp_path, script: str) -> ProductCredentialSourceCatalog:
    models = ShortcutModelsConfig.model_validate(_models({"argv": [sys.executable, "-c", script]}))
    return ProductCredentialSourceCatalog(models, oauth_root=tmp_path / "oauth")


def test_config_load_never_executes_helper(tmp_path, monkeypatch) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    (user_root / "config.yaml").write_text(
        "models:\n"
        "  mode: shortcut\n"
        "  default:\n"
        "    model: claude-sonnet-4-8\n"
        "  api_key_helper:\n"
        f"    argv: [{sys.executable!r}, -c, 'print(\"secret\")']\n",
        encoding="utf-8",
    )
    calls = 0

    async def unexpected(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("config parsing must not start a process")

    monkeypatch.setattr("mote.product.models.credential_sources.run_verified_fixed_argv", unexpected)

    config = load_config(reload=True, user_config_root=user_root)

    assert config.models.api_key_helper is not None
    assert calls == 0


@pytest.mark.parametrize(
    "source",
    [
        ConfigSource.SYSTEM,
        ConfigSource.WORKDIR,
        ConfigSource.PROFILE,
        ConfigSource.ENV,
        ConfigSource.CLI_FLAG,
        ConfigSource.PROGRAMMATIC,
    ],
)
def test_only_user_and_managed_layers_can_declare_helper(source) -> None:
    stack = ConfigLayerStack()
    stack.add(
        ConfigLayer(
            source=source,
            data={"models": _models({"argv": [sys.executable, "-c", "print('x')"]})},
            trusted=True,
        )
    )

    assert "api_key_helper" not in stack.effective()["models"]


@pytest.mark.parametrize("source", [ConfigSource.USER, ConfigSource.MANAGED])
def test_user_and_managed_helper_survives_with_provenance(source) -> None:
    stack = ConfigLayerStack()
    stack.add(
        ConfigLayer(
            source=source,
            data={"models": _models({"argv": [sys.executable, "-c", "print('x')"]})},
            trusted=True,
        )
    )

    assert stack.effective()["models"]["api_key_helper"]["argv"][0] == sys.executable
    assert stack.provenance()["models.api_key_helper.argv"] == source.name


@pytest.mark.asyncio
async def test_helper_runs_as_fixed_argv_during_credential_activation(tmp_path) -> None:
    models = ShortcutModelsConfig.model_validate(_models({"argv": [sys.executable, "-c", "print('sk-from-helper')"]}))
    catalog = ProductCredentialSourceCatalog(models, oauth_root=tmp_path / "oauth")

    handle = await catalog.create_handle(
        "endpoint:default:key:0",
        "endpoint:default",
        "endpoint:default:key:0",
    )
    lease = await handle.acquire()
    material = await lease.resolve()

    assert (
        material.read_for_wire(CredentialWireAccess("endpoint:default", "endpoint:default:key:0")) == "sk-from-helper"
    )
    material.release()
    await lease.release()
    await handle.aclose()


@pytest.mark.asyncio
async def test_static_key_prevents_helper_execution(tmp_path, monkeypatch) -> None:
    models = ShortcutModelsConfig.model_validate(
        _models({"argv": [sys.executable, "-c", "print('unused')"]}, api_key="sk-static")
    )
    calls = 0

    async def unexpected(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("helper must not execute")

    monkeypatch.setattr("mote.product.models.credential_sources.run_verified_fixed_argv", unexpected)
    catalog = ProductCredentialSourceCatalog(models, oauth_root=tmp_path / "oauth")
    await catalog.create_handle("endpoint:default:key:0", "endpoint:default", "endpoint:default:key:0")

    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "limit", "timeout", "disposition"),
    [
        (
            "import sys; print('secret-out'); print('secret-err', file=sys.stderr); sys.exit(3)",
            65_536,
            30.0,
            "exited",
        ),
        ("print('secret' * 1000)", 32, 30.0, "output_limit"),
        ("import os; os.write(1, b'\\xff')", 65_536, 30.0, "output_decode_failed"),
        (
            "import time; print('secret', flush=True); time.sleep(10)",
            65_536,
            0.02,
            "timed_out",
        ),
    ],
)
async def test_helper_failures_are_typed_and_secret_output_is_not_disclosed(
    tmp_path, monkeypatch, script, limit, timeout, disposition
) -> None:
    monkeypatch.setattr("mote.product.models.credential_sources._HELPER_MAX_OUTPUT_BYTES", limit)
    monkeypatch.setattr("mote.product.models.credential_sources._HELPER_TIMEOUT_SECONDS", timeout)
    catalog = _helper_catalog(tmp_path, script)

    with pytest.raises(RuntimeError) as raised:
        await catalog.create_handle(
            "endpoint:default:key:0",
            "endpoint:default",
            "endpoint:default:key:0",
        )

    assert disposition in str(raised.value)
    assert "secret-out" not in str(raised.value)
    assert "secret-err" not in str(raised.value)


def test_helper_requires_structured_absolute_argv() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        ShortcutModelsConfig.model_validate(_models({"argv": ["sh", "-c", "echo unsafe"]}))
    with pytest.raises(ValueError):
        ShortcutModelsConfig.model_validate(_models("echo unsafe"))


def test_api_key_helper_is_stripped_from_untrusted_layers() -> None:
    assert "api_key_helper" in CREDENTIAL_DENYLIST
    cleaned = strip_sensitive({"api_key_helper": {"argv": ["/bin/false"]}, "proxy": "ok"})
    assert "api_key_helper" not in cleaned
    assert cleaned["proxy"] == "ok"
