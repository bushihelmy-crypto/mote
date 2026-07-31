"""Probe PyPI wheel availability for every frozen inference target cell."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "zdocs" / "parity" / "dependency-plan-v1.yaml"
LOCK = ROOT / "requirements" / "inference.lock"
MATRIX = ROOT / "zdocs" / "parity" / "dependency-platform-matrix-v1.yaml"
PLATFORM_TAGS = {
    "linux_x86_64": ("manylinux2014_x86_64",),
    "linux_aarch64": ("manylinux2014_aarch64",),
    "macos_x86_64": ("macosx_10_9_x86_64", "macosx_10_13_x86_64", "macosx_11_0_universal2"),
    "macos_arm64": ("macosx_11_0_arm64", "macosx_11_0_universal2"),
    "windows_x86_64": ("win_amd64",),
}


def _locked() -> dict[str, str]:
    locked = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement = line.split(" --hash", 1)[0]
        name, version = requirement.split("==", 1)
        locked[name.lower().replace("_", "-")] = version
    return locked


def _probe(requirement: str, python_version: str, platform_tags: tuple[str, ...]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="mote-wheel-probe-") as destination:
        python_parts = python_version.split(".")
        python_digits = python_parts[0] + python_parts[1]
        resolver_version = python_digits
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--no-deps",
            "--disable-pip-version-check",
            "--dest",
            destination,
            "--python-version",
            resolver_version,
            "--implementation",
            "cp",
            "--abi",
            "cp" + python_digits,
            "--abi",
            "abi3",
        ]
        for platform_tag in platform_tags:
            command.extend(("--platform", platform_tag))
        command.append(requirement)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        summary = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "no pip output"
        return completed.returncode == 0, summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=MATRIX)
    parser.add_argument("--component", action="append", default=[])
    arguments = parser.parse_args()
    matrix = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
    plan = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    locked = _locked()
    failed = False
    selected = set(arguments.component)
    for component in plan["components"]:
        name = component["name"]
        if selected and name not in selected:
            continue
        version = locked[name.lower().replace("_", "-")]
        cells = []
        targets = matrix["platforms"] if component["native_binary"] else ["any"]
        for python_version in matrix["python_versions"]:
            for target in targets:
                if target == "any":
                    ok, detail = _probe(f"{name}=={version}", python_version, ("manylinux2014_x86_64",))
                else:
                    ok, detail = _probe(f"{name}=={version}", python_version, PLATFORM_TAGS[target])
                cells.append({"python": python_version, "platform": target, "available": ok, "detail": detail})
                failed = failed or not ok
        matrix["components"][name] = {
            "status": "passed" if all(cell["available"] for cell in cells) else "failed",
            "verified_cells": cells,
        }
    all_passed = all(item["status"] == "passed" for item in matrix["components"].values())
    matrix["gate_status"] = "passed" if all_passed else "failed"
    arguments.output.write_text(yaml.safe_dump(matrix, sort_keys=False), encoding="utf-8")
    print(json.dumps({"gate_status": matrix["gate_status"], "output": str(arguments.output)}))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
