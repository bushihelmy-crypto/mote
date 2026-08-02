from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _explicit_product_config_root(tmp_path, monkeypatch):
    from mote.product.config import loader
    from mote.product.config.sources import discover_source_files
    from mote.product.paths import default_runtime_paths

    package_dir = default_runtime_paths().package_data_root
    user_config_root = tmp_path / "product-config"
    user_config_root.mkdir()
    (user_config_root / "config.yaml").write_text(
        (package_dir / "config.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def discover(cwd=None, **kwargs):
        kwargs.setdefault("user_config_root", user_config_root)
        return discover_source_files(cwd, **kwargs)

    monkeypatch.setattr(loader, "discover_source_files", discover)
    return user_config_root
