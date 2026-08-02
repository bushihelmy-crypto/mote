from pathlib import Path


def test_media_provider_composition_uses_closed_config_union() -> None:
    root = Path(__file__).parents[2]
    registry = (root / "product/media_generation/registry.py").read_text(encoding="utf-8")
    service = (root / "product/media_generation/service.py").read_text(encoding="utf-8")

    assert "config: Any" not in registry
    assert "getattr(config" not in registry
    assert "self.providers" not in registry
    assert "MappingProxyType" in registry
    assert "MediaProviderConfig" in registry
    assert "getattr(self._multimodal" not in service
    assert "getattr(multimodal" not in service
