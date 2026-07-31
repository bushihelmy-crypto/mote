"""Verify the frozen inference dependency and supply-chain plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import tomllib
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "zdocs" / "parity" / "dependency-plan-v1.yaml"
REQUIRED_SCOPES = {"current_embedded", "current_shared"}


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _lock_entries(path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, payload = line.partition("==")
        if not separator:
            raise ValueError(f"invalid lock entry at line {line_number}")
        version, marker, digest = payload.partition(" --hash=sha256:")
        if not marker or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"lock entry lacks a sha256 artifact digest at line {line_number}")
        normalized = name.strip().lower().replace("_", "-")
        if normalized in entries:
            raise ValueError(f"duplicate lock entry for {normalized}")
        entries[normalized] = {"version": version.strip(), "artifact_digest": "sha256:" + digest}
    return entries


def verify(plan_path: Path, lock_path: Path, sbom_path: Path) -> dict[str, Any]:
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    lock = _lock_entries(lock_path)
    components = plan["components"]
    names = [component["name"].lower().replace("_", "-") for component in components]
    if len(names) != len(set(names)):
        raise ValueError("dependency plan contains duplicate package names")
    if set(names) != set(lock):
        raise ValueError("lockfile package set differs from the dependency plan")
    owners = [component["owner"] for component in components]
    if len(owners) != len(set(owners)):
        raise ValueError("one production client owner must own each dependency")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    core_names = {
        requirement.split(";", 1)[0]
        .strip()
        .split("[", 1)[0]
        .split("=", 1)[0]
        .split("<", 1)[0]
        .split(">", 1)[0]
        .lower()
        .replace("_", "-")
        for requirement in project["dependencies"]
    }

    sbom_components = []
    for component in components:
        name = component["name"].lower().replace("_", "-")
        scopes = set(component.get("release_scope", []))
        if not scopes or not scopes <= REQUIRED_SCOPES:
            raise ValueError(f"{name} has invalid or absent release scope")
        if component["optional"]:
            extra = component.get("extra")
            if extra not in extras or not any(
                requirement.lower().replace("_", "-").startswith(name) for requirement in extras[extra]
            ):
                raise ValueError(f"{name} is not declared by its {extra!r} packaging extra")
        elif name not in core_names:
            raise ValueError(f"required dependency {name} is absent from project dependencies")
        locked = lock[name]
        if Version(locked["version"]) not in SpecifierSet(component["version_range"]):
            raise ValueError(f"{name} {locked['version']} is outside {component['version_range']}")
        sbom_components.append(
            {
                "type": "library",
                "name": component["name"],
                "version": locked["version"],
                "purl": component["sbom_component"] + "@" + locked["version"],
                "hashes": [{"alg": "SHA-256", "content": locked["artifact_digest"].removeprefix("sha256:")}],
                "licenses": [{"license": {"id": component["license"]}}],
                "properties": [
                    {"name": "mote:owner", "value": component["owner"]},
                    {"name": "mote:release_scope", "value": ",".join(component["release_scope"])},
                ],
            }
        )
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"properties": [{"name": "mote:dependency-plan-digest", "value": _digest(plan_path)}]},
        "components": sbom_components,
    }
    sbom_path.parent.mkdir(parents=True, exist_ok=True)
    sbom_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--sbom", type=Path)
    arguments = parser.parse_args()
    plan = yaml.safe_load(arguments.plan.read_text(encoding="utf-8"))
    lock = arguments.lock or ROOT / plan["lockfile"]
    sbom = arguments.sbom or ROOT / "zdocs" / "parity" / "inference-sbom-v1.cdx.json"
    try:
        verify(arguments.plan, lock, sbom)
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"verified {lock} -> {sbom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
