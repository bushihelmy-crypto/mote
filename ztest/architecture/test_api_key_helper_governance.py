from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_config_parsing_has_no_helper_execution() -> None:
    loader = (ROOT / "product/config/loader.py").read_text(encoding="utf-8")
    secrets = (ROOT / "product/config/secrets.py").read_text(encoding="utf-8")

    assert "resolve_api_key" not in loader
    assert "subprocess" not in secrets
    assert "shell=True" not in secrets


def test_helper_uses_only_fixed_argv_runner() -> None:
    source = (ROOT / "product/models/credential_sources.py").read_text(encoding="utf-8")

    assert "run_verified_fixed_argv(" in source
    assert "run_authorized_shell" not in source
    assert "subprocess" not in source
