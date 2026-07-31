"""Run deterministic source mutants against focused protocol oracles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "zdocs" / "parity" / "mutation-ratchet-result-v1.json"

MUTANTS = (
    {
        "id": "translation.openai_choice_cardinality",
        "path": "product/models/transports/translation.py",
        "old": "len(choices) != 1",
        "new": "len(choices) < 1",
        "test": "ztest/inference/test_transport_translation.py",
        "ratchet": "translation_mutation",
    },
    {
        "id": "translation.tool_arguments_object",
        "path": "product/models/transports/translation.py",
        "old": 'if not isinstance(arguments, dict):\n            raise ValueError("OpenAI tool arguments must be an object")',
        "new": 'if arguments is None:\n            raise ValueError("OpenAI tool arguments must be an object")',
        "test": "ztest/inference/test_transport_translation.py",
        "ratchet": "translation_mutation",
    },
    {
        "id": "validator.byte_limit_fail_closed",
        "path": "product/models/transports/validator.py",
        "old": "if self._bytes > self._max_bytes:",
        "new": "if False:",
        "test": "ztest/inference/test_response_validator_limits.py",
        "ratchet": "response_validator_mutation",
    },
    {
        "id": "validator.frame_limit_fail_closed",
        "path": "product/models/transports/validator.py",
        "old": "if self._frames > self._max_frames:",
        "new": "if False:",
        "test": "ztest/inference/test_response_validator_limits.py",
        "ratchet": "response_validator_mutation",
    },
    {
        "id": "failure.unknown_commit_requires_reconcile",
        "path": "contracts/model/failover.py",
        "old": "if self.external_commit_state is ExternalCommitState.UNKNOWN:",
        "new": "if False:",
        "test": "ztest/contracts/test_inference_gateway_contracts.py",
        "ratchet": "failure_mapping_mutation",
    },
)


def _run(mutant: dict[str, str]) -> dict[str, Any]:
    source = ROOT / mutant["path"]
    original = source.read_text(encoding="utf-8")
    if original.count(mutant["old"]) != 1:
        raise ValueError(f"mutant anchor is not unique: {mutant['id']}")
    temporary = tempfile.TemporaryDirectory(prefix="mote-mutant-")
    overlay = Path(temporary.name) / "mote"
    shutil.copytree(ROOT, overlay, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    mutated_path = overlay / mutant["path"]
    mutated_path.write_text(original.replace(mutant["old"], mutant["new"]), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = temporary.name
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", mutant["test"], "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=overlay,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    temporary.cleanup()
    return {
        "id": mutant["id"],
        "ratchet": mutant["ratchet"],
        "status": "killed" if completed.returncode != 0 else "survived",
        "test": mutant["test"],
        "exit_code": completed.returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=RESULT)
    arguments = parser.parse_args()
    try:
        mutations = [_run(mutant) for mutant in MUTANTS]
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    scores = {}
    for ratchet in {mutant["ratchet"] for mutant in MUTANTS}:
        selected = [mutation for mutation in mutations if mutation["ratchet"] == ratchet]
        scores[ratchet] = sum(item["status"] == "killed" for item in selected) / len(selected)
    document = {
        "schema_version": 1,
        "revision": "mutation-ratchet-result-v1",
        "mutations": mutations,
        "scores": scores,
        "gate_status": "passed" if all(item["status"] == "killed" for item in mutations) else "failed",
    }
    arguments.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(arguments.output)
    return 0 if document["gate_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
