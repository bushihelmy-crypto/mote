"""Generate a local candidate SLO sample without freezing release thresholds."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Awaitable, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mote.contracts.inference.generation_artifact import GenerationArtifact
from mote.contracts.inference.identity import TrustedSchedulingClass
from mote.contracts.inference.persisted_event import PersistedLifecycleEvent
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.product.inference.security.wire_permit import (
    Ed25519WirePermitSigner,
    Ed25519WirePermitVerifier,
    canonical_wire_permit_payload,
)
from mote.runtime.inference.fair_queue import FairAdmissionQueue
from mote.runtime.inference.generation import GatewayGenerationOwner, GenerationDomain

ROOT = Path(__file__).resolve().parents[2]
SLO = ROOT / "zdocs" / "parity" / "inference-slo-v1.yaml"


def _artifact() -> GenerationArtifact:
    return GenerationArtifact(
        generation_id="slo-generation",
        model_planner_and_bindings={},
        service_planner_and_bindings={},
        session_capability_and_bindings={},
        transfer_capability_and_bindings={},
        credential_versions={},
        transport_registry_revision="transport-v1",
        client_profile_revision="client-v1",
        failure_policy_revision="failure-v2",
        capability_catalog_pricing_snapshot={},
        governance_cache_plugin_revisions={},
        required_wire_contract_range=(1, 1),
        activation_policy={},
        min_reader_version=1,
        min_writer_version=1,
        persistence_schema_versions={"receipt": 1},
        migration_set_digest="sha256:" + "a" * 64,
        artifact_digest="sha256:" + "b" * 64,
        signer_key_id="slo-key",
        signature="candidate",
    )


def _permit() -> WirePermit:
    now = datetime.now(timezone.utc)
    return WirePermit(
        attempt_id="slo-attempt",
        execution_taxonomy=ExecutionTaxonomy.UNARY_FINITE_ATTEMPT,
        owner_journal_id="slo-journal",
        wire_unit="http_request",
        generation_id="slo-generation",
        generation_artifact_digest="sha256:" + "b" * 64,
        ordinal=1,
        nonce="0123456789abcdef",
        issued_journal_revision=1,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="slo-key",
        audience="slo",
        trust_revision=1,
        backup_epoch=0,
        admission_epoch=0,
        signature="unsigned",
    )


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    rank = max(1, (len(ordered) * int(percentile * 1000) + 999) // 1000)
    return ordered[min(rank, len(ordered)) - 1]


def _summary(samples: list[int], unit: str) -> dict[str, float | int | str]:
    divisor = 1_000 if unit == "us" else 1_000_000
    return {
        "unit": unit,
        "samples": len(samples),
        "p50": _nearest_rank(samples, 0.5) / divisor,
        "p99": _nearest_rank(samples, 0.99) / divisor,
        "p99_9": _nearest_rank(samples, 0.999) / divisor,
        "median": median(samples) / divisor,
    }


def _sync_samples(operation: Callable[[], None], warmup: int, iterations: int) -> list[int]:
    for _ in range(warmup):
        operation()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        samples.append(time.perf_counter_ns() - started)
    return samples


async def _async_samples(operation: Callable[[], Awaitable[None]], warmup: int, iterations: int) -> list[int]:
    for _ in range(warmup):
        await operation()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        await operation()
        samples.append(time.perf_counter_ns() - started)
    return samples


async def measure(warmup: int, iterations: int) -> dict[str, object]:
    owner = GatewayGenerationOwner()
    artifact = _artifact()
    owner.stage(artifact)
    owner.activate(artifact.generation_id, artifact.artifact_digest)

    def generation() -> None:
        owner.acquire(GenerationDomain.MODEL).release()

    scheduling = TrustedSchedulingClass(tenant_weight=1, project_weight=1, cost_units=1, priority=0)
    queue_sequence = 0

    async def queue_round_trip() -> None:
        nonlocal queue_sequence
        queue_sequence += 1
        queue = FairAdmissionQueue(capacity=1)
        deadline = asyncio.get_running_loop().time() + 1
        await queue.enqueue(queue_sequence, tenant_id="slo", project_id="slo", scheduling=scheduling, deadline=deadline)
        await queue.dequeue()
        await queue.task_done()

    private_key = Ed25519PrivateKey.generate()
    signer = Ed25519WirePermitSigner(issuer_key_id="slo-key", trust_revision=1, private_key=private_key)
    verifier = Ed25519WirePermitVerifier({("slo-key", 1): private_key.public_key()})
    unsigned = _permit()
    signed = signer.sign(unsigned)

    def canonicalize() -> None:
        canonical_wire_permit_payload(unsigned)

    async def sign_verify() -> None:
        candidate = signer.sign(unsigned)
        if not await verifier.verify(candidate):
            raise RuntimeError("candidate permit verification failed")

    authority_directory = tempfile.TemporaryDirectory(prefix="mote-slo-")
    authority_path = Path(authority_directory.name) / "authority.sqlite3"
    receipt_store = SQLiteAttemptReceiptStore(authority_path)
    await receipt_store.initialize()
    receipt_sequence = 0

    async def receipt_transaction() -> None:
        nonlocal receipt_sequence
        receipt_sequence += 1
        identity = f"slo-{receipt_sequence}"
        receipt = AttemptReceipt(
            attempt_id=identity,
            generation_id="slo-generation",
            generation_artifact_digest="sha256:" + "b" * 64,
            revision=1,
            state=ReceiptState.ACCEPTED,
            fencing_token=1,
            request_digest="sha256:" + "c" * 64,
            operation="chat.complete",
            idempotency_class="attempt",
        )
        await receipt_store.accept(receipt)

    event_sequence = 0

    async def event_persistence() -> None:
        nonlocal event_sequence
        event_sequence += 1
        await receipt_store.append_event(
            PersistedLifecycleEvent(
                execution_id=f"slo-event-{event_sequence}",
                sequence=1,
                receipt_revision=1,
                event_type="candidate",
                payload=b"{}",
                terminal=True,
            )
        )

    results = {
        "generation_acquire_release_us": _summary(_sync_samples(generation, warmup, iterations), "us"),
        "queue_enqueue_dequeue_us": _summary(await _async_samples(queue_round_trip, warmup, iterations), "us"),
        "permit_canonicalize_us": _summary(_sync_samples(canonicalize, warmup, iterations), "us"),
        "permit_sign_verify_us": _summary(await _async_samples(sign_verify, warmup, iterations), "us"),
        "receipt_transaction_ms": _summary(await _async_samples(receipt_transaction, warmup, iterations), "ms"),
        "event_persistence_ms": _summary(await _async_samples(event_persistence, warmup, iterations), "ms"),
    }
    assert await verifier.verify(signed)
    authority_directory.cleanup()
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--output", type=Path, default=ROOT / "zdocs" / "parity" / "inference-slo-candidate-v1.json")
    arguments = parser.parse_args()
    if arguments.warmup < 0 or arguments.iterations < 1000:
        print("warmup must be non-negative and iterations must be at least 1000", file=sys.stderr)
        return 2
    results = asyncio.run(measure(arguments.warmup, arguments.iterations))
    slo_digest = "sha256:" + hashlib.sha256(SLO.read_bytes()).hexdigest()
    document = {
        "schema_version": 1,
        "slo_revision": "inference-slo-v1",
        "slo_protocol_digest": slo_digest,
        "status": "candidate_not_frozen",
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "cpu_count": os.cpu_count(),
        },
        "warmup_iterations": arguments.warmup,
        "measured_iterations": arguments.iterations,
        "clock": "perf_counter_ns",
        "quantile_method": "nearest_rank",
        "dimensions": results,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
