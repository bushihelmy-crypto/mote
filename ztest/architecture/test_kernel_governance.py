from pathlib import Path

from mote.ztest.architecture.import_scanner import scan_tree

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = {"commands", "execution", "inference", "output", "telemetry", "tools"}
ALLOWED = {
    "commands": {"output", "telemetry"},
    "execution": {"commands", "inference", "output", "telemetry"},
    "inference": {"output", "telemetry"},
    "output": set(),
    "telemetry": set(),
    "tools": set(),
}


def test_kernel_has_only_capability_packages_at_root():
    assert [path.name for path in (ROOT / "kernel").glob("*.py")] == ["__init__.py"]
    actual = {path.name for path in (ROOT / "kernel").iterdir() if path.is_dir() and any(path.rglob("*.py"))}
    assert actual == CAPABILITIES


def test_kernel_never_imports_higher_layers_and_obeys_capability_dag():
    violations = []
    for edge in scan_tree(ROOT):
        if edge.target_module.startswith(("mote.runtime", "mote.orchestration", "mote.product")):
            violations.append(edge)
        source = edge.source_module.split(".")[2:3]
        target = edge.target_module.split(".")[2:3]
        if (
            edge.target_module.startswith("mote.kernel.")
            and source
            and target
            and source[0] in CAPABILITIES
            and target[0] in CAPABILITIES
        ):
            if source[0] != target[0] and target[0] not in ALLOWED[source[0]]:
                violations.append(edge)
    assert not violations


def test_kernel_has_no_legacy_or_generic_package_names():
    forbidden = {"flow", "think", "models", "parser", "prompt", "common", "shared", "utils", "misc", "helpers"}
    active = {path.name for path in (ROOT / "kernel").iterdir() if path.is_dir() and any(path.rglob("*.py"))}
    assert not forbidden.intersection(active)
