"""Reject unreviewed migration diagnostics for configured type islands."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "typecheck/migration-diagnostics.json"
CONFIGS = tuple(sorted((ROOT / "typecheck").glob("pyright.*.json")))
PYRIGHT_PROJECT_FLAG = "--project"


def _fingerprint(diagnostic: dict[str, object]) -> str:
    location = diagnostic["range"]
    assert isinstance(location, dict)
    start = location["start"]
    assert isinstance(start, dict)
    message = str(diagnostic["message"]).splitlines()[0]
    payload = "|".join(
        (
            str(diagnostic.get("rule")),
            str(start["line"]),
            str(start["character"]),
            message,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _actual_diagnostics() -> list[dict[str, str]]:
    actual: list[dict[str, str]] = []
    environment = os.environ.copy()
    environment["PYRIGHT_PYTHON_FORCE_VERSION"] = "latest"
    for config in CONFIGS:
        completed = subprocess.run(
            [
                "pyright",
                "--outputjson",
                PYRIGHT_PROJECT_FLAG,
                str(config.relative_to(ROOT)),
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if not completed.stdout.strip():
            detail = completed.stderr.strip() or "pyright produced no JSON output"
            raise SystemExit(f"pyright failed for {config.name} (exit {completed.returncode}): {detail}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid pyright JSON for {config.name}: {exc}") from exc
        for diagnostic in result["generalDiagnostics"]:
            if diagnostic["severity"] != "error":
                continue
            actual.append(
                {
                    "file": str(Path(diagnostic["file"]).resolve().relative_to(ROOT)),
                    "rule": diagnostic.get("rule") or "",
                    "fingerprint": _fingerprint(diagnostic),
                }
            )
    return sorted(actual, key=lambda item: (item["file"], item["fingerprint"]))


def main() -> int:
    entries = json.loads(MANIFEST.read_text())["diagnostics"]
    required = {"file", "rule", "fingerprint", "justification", "owner", "expiry"}
    for entry in entries:
        missing = required - entry.keys()
        if missing:
            raise SystemExit(f"migration diagnostic is missing {sorted(missing)}")
        if date.fromisoformat(entry["expiry"]) < date.today():
            raise SystemExit(f"expired migration diagnostic: {entry['file']}")
    expected = sorted(
        (
            {
                "file": entry["file"],
                "rule": entry["rule"],
                "fingerprint": entry["fingerprint"],
            }
            for entry in entries
        ),
        key=lambda item: (item["file"], item["fingerprint"]),
    )
    actual = _actual_diagnostics()
    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
