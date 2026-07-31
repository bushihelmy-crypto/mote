"""Generate the specification-to-executable-gate evidence map."""

from __future__ import annotations

import json

SECTION_12 = {
    "12.1": ["EVENT-TRANSFORMATION"],
    "12.2": ["EVENT-FACT-ADMISSION", "DURABLE-POLICY", "STORE-CUTOVER"],
    "12.3": ["MIGRATION-DEBT", "STORE-CUTOVER"],
    "12.4": ["RESTORE-ADMISSION"],
    "12.5": ["SUBSCRIPTION-RELIABILITY"],
    "12.6": ["PRODUCT-IMPORT-SCC", "PRODUCT-OWNER-EXPORT"],
    "12.7": ["ARCH-LOCAL-IMPORT", "TYPE-TELEMETRY-ERASURE", "DYNAMIC-DISCOVERY", "TYPE-GOVERNED-BOUNDARY"],
    "12.8": ["UNIQUE-PRODUCTION-PATH", "MIGRATION-DEBT"],
    "12.9": ["CAP-REACHABILITY", "PRODUCT-OWNER-EXPORT"],
    "12.10": ["WIRE-AUTHORITY", "DERIVED-ARTIFACT"],
}

SECTION_16 = {
    "1": ["EVENT-TRANSFORMATION"],
    "2": ["EVENT-TRANSFORMATION"],
    "3": ["EVENT-TRANSFORMATION"],
    "4": ["TYPE-TELEMETRY-ERASURE"],
    "5": ["EVENT-FACT-ADMISSION"],
    "6": ["STORE-CUTOVER", "MIGRATION-DEBT"],
    "7": ["STORE-CUTOVER", "UNIQUE-PRODUCTION-PATH"],
    "8": ["MIGRATION-DEBT", "UNIQUE-PRODUCTION-PATH"],
    "9": ["STORE-CUTOVER", "RESTORE-ADMISSION"],
    "10": ["STORE-CUTOVER", "MIGRATION-DEBT"],
    "11": ["RESTORE-ADMISSION"],
    "12": ["DURABLE-POLICY"],
    "13": ["SUBSCRIPTION-RELIABILITY"],
    "14": ["EVENT-TRANSFORMATION", "SUBSCRIPTION-RELIABILITY"],
    "15": ["PRODUCT-IMPORT-SCC", "PRODUCT-OWNER-EXPORT"],
    "16": ["PRODUCT-OWNER-EXPORT"],
    "17": ["PRODUCT-OWNER-EXPORT", "UNIQUE-PRODUCTION-PATH"],
    "18": ["ARCH-LOCAL-IMPORT", "TYPE-GOVERNED-BOUNDARY", "DYNAMIC-DISCOVERY"],
    "19": ["UNIQUE-PRODUCTION-PATH"],
    "20": ["MIGRATION-DEBT", "UNIQUE-PRODUCTION-PATH"],
    "21": ["UNIQUE-PRODUCTION-PATH"],
    "22": ["CAP-REACHABILITY"],
    "23": ["CAP-REACHABILITY", "PRODUCT-OWNER-EXPORT"],
    "24": ["CAP-REACHABILITY", "DYNAMIC-DISCOVERY"],
    "25": ["DERIVED-ARTIFACT"],
    "26": ["DERIVED-ARTIFACT"],
    "27": ["WIRE-AUTHORITY"],
    "28": ["DERIVED-ARTIFACT"],
    "29": ["STORE-CUTOVER", "MIGRATION-DEBT"],
}


def build() -> dict[str, object]:
    return {
        "schema": "dynamic-boundary-requirement-evidence-v1",
        "generator": "ztest/architecture/requirement_evidence.py",
        "runtime_input": False,
        "authority": "zdocs/dynamic-boundary-product-event-governance-action.md",
        "gate_status_index": "zdocs/architecture/gate-status/index.json",
        "section_12": SECTION_12,
        "section_13": {
            "declarations": "product/composition/gates.py",
            "runner": "ztest/architecture/gate_status.py",
            "raw_evidence_directory": "zdocs/architecture/gate-evidence",
            "generated_status_directory": "zdocs/architecture/gate-status",
        },
        "section_16": SECTION_16,
    }


def main() -> int:
    print(json.dumps(build(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
