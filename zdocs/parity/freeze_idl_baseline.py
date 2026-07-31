"""Freeze or verify exact Gate 0 API/IDL source digests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "zdocs" / "parity" / "idl-baseline-v1.json"
IDL_PATHS = (
    "zdocs/parity/api/inference-v1.openapi.yaml",
    "zdocs/parity/api/admin-v1.openapi.yaml",
    "zdocs/parity/api/realtime-webhook-v1.asyncapi.yaml",
    "zdocs/parity/rpc/gateway-v1.proto",
)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def current() -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": "idl-baseline-v1",
        "compatibility_policy": "exact_until_versioned_change_request",
        "artifacts": {path: _digest(ROOT / path) for path in IDL_PATHS},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()
    candidate = current()
    if arguments.write:
        BASELINE.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(BASELINE)
        return 0
    if not BASELINE.is_file():
        print("IDL baseline is missing", file=sys.stderr)
        return 1
    frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
    if frozen != candidate:
        print("breaking or unreviewed IDL drift detected", file=sys.stderr)
        return 1
    print("IDL baseline verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
