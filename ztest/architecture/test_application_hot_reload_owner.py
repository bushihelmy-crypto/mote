from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_product_has_one_reload_coordinator_composition_site() -> None:
    definitions = []
    constructions = []
    for path in (ROOT / "product").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "class ApplicationReloadCoordinator" in source:
            definitions.append(path.relative_to(ROOT).as_posix())
        if "ApplicationReloadCoordinator(" in source and "class ApplicationReloadCoordinator" not in source:
            constructions.append(path.relative_to(ROOT).as_posix())
    assert definitions == ["product/composition/model_reload.py"]
    assert constructions == ["product/composition/bootstrap.py"]


def test_candidate_build_swap_and_drain_are_separate_phases() -> None:
    reload_source = (ROOT / "product/composition/model_reload.py").read_text(encoding="utf-8")
    owner = (ROOT / "product/composition/model_application.py").read_text(encoding="utf-8")
    assert "build_application_candidate(" in reload_source
    assert "ExpectedActive(expected_id)" in reload_source
    assert "candidate.approved_capabilities - self._current.approved_capabilities" in owner
    assert "candidate.trust_revision != self._current.trust_revision" in owner
    assert "old.retired_at" in owner
    assert "_release(self, generation" in owner
