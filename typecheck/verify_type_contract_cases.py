"""Verify pyright positive and negative type-contract cases exactly."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPECHECK = ROOT / "typecheck"


def _run(*paths: Path) -> tuple[int, dict[str, object]]:
    completed = subprocess.run(
        ["pyright", "--outputjson", *(str(path) for path in paths)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode, json.loads(completed.stdout)


def main() -> int:
    pass_files = sorted((TYPECHECK / "cases/pass").glob("*.py"))
    fail_files = sorted((TYPECHECK / "cases/fail").glob("*.py"))
    pass_code, pass_result = _run(*pass_files)
    if pass_code != 0:
        print(json.dumps(pass_result, indent=2))
        return 1

    fail_results: list[dict[str, object]] = []
    for fail_file in fail_files:
        fail_code, fail_result = _run(fail_file)
        if fail_code == 0:
            print(f"negative type-contract case unexpectedly passed: {fail_file}")
            return 1
        fail_results.append(fail_result)
    actual = [
        {
            "file": str(Path(item["file"]).resolve().relative_to(ROOT)),
            "rule": item.get("rule"),
            "line": item["range"]["start"]["line"] + 1,
        }
        for fail_result in fail_results
        for item in fail_result["generalDiagnostics"]
        if item["severity"] == "error"
    ]
    actual.sort(key=lambda item: (item["file"], item["line"], item["rule"] or ""))
    expected = sorted(
        json.loads((TYPECHECK / "case-expectations.json").read_text())["cases"],
        key=lambda item: (item["file"], item["line"], item["rule"]),
    )
    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
