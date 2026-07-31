"""Measure an authenticated same-host Shared Process unary RPC candidate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mote.contracts.inference.shared import SharedHandshake
from mote.product.inference.daemon.generation import SharedGenerationBackend
from mote.product.inference.daemon.grpc_client import SharedGrpcClient
from mote.product.inference.daemon.grpc_server import SharedGrpcServer
from mote.product.inference.daemon.rpc import gateway_v1_pb2 as pb
from mote.product.inference.daemon.security import SharedHandshakeAuthority, current_incarnation, sign_handshake
from mote.runtime.inference.generation import GatewayGenerationOwner

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "zdocs" / "parity" / "shared-rpc-slo-candidate-v1.json"
DIGEST = "sha256:" + "8" * 64


class _Backend:
    async def start_unary(self, request, credential):
        return 1


def _rank(values: list[int], percentile: float) -> int:
    values.sort()
    index = max(0, min(len(values) - 1, (int(len(values) * percentile + 0.999999) - 1)))
    return values[index]


async def _measure(warmup: int, iterations: int) -> dict[str, object]:
    secret = b"application-secret"
    authority = SharedHandshakeAuthority(
        socket_generation="slo-socket",
        application_keys={"slo": ("application-key", secret)},
        session_key_id="session-key",
        session_key=b"session-secret",
        current_protocol_version=2,
    )
    with tempfile.TemporaryDirectory(prefix="mote-rpc-slo-") as directory:
        socket_path = Path(directory) / "gateway.sock"
        server = SharedGrpcServer(
            socket_path=socket_path,
            authority=authority,
            backend=_Backend(),
            generations=SharedGenerationBackend(GatewayGenerationOwner()),
            readiness=lambda: (True, {"admission": "ready"}),
        )
        await server.start()
        client = SharedGrpcClient(socket_path)
        now = datetime.now(timezone.utc)
        unsigned = SharedHandshake(
            protocol_versions=(2,),
            application_id="slo",
            caller=current_incarnation(os.getpid()),
            socket_generation="slo-socket",
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="policy-v1",
            delegation_digest=DIGEST,
            nonce=os.urandom(16).hex(),
            issued_at=now,
            expires_at=now + timedelta(minutes=2),
            key_id="application-key",
            signature="unsigned",
        )
        await client.authenticate(sign_handshake(unsigned, secret))
        sequence = 0

        async def call() -> None:
            nonlocal sequence
            sequence += 1
            await client.start_unary(
                pb.StartRequest(
                    envelope=client.envelope(idempotency_key=f"slo:{sequence}"),
                    execution_id=f"slo-{sequence}",
                    operation="chat.complete",
                )
            )

        try:
            for _ in range(warmup):
                await call()
            samples = []
            for _ in range(iterations):
                started = time.perf_counter_ns()
                await call()
                samples.append(time.perf_counter_ns() - started)
        finally:
            await client.close()
            await server.stop(grace_seconds=0)
    return {
        "unit": "ms",
        "samples": iterations,
        "p50": _rank(samples.copy(), 0.5) / 1_000_000,
        "p99": _rank(samples.copy(), 0.99) / 1_000_000,
        "p99_9": _rank(samples.copy(), 0.999) / 1_000_000,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=1000)
    arguments = parser.parse_args()
    if arguments.iterations < 1000:
        return 2
    result = asyncio.run(_measure(arguments.warmup, arguments.iterations))
    document = {
        "schema_version": 1,
        "revision": "shared-rpc-slo-candidate-v1",
        "status": "candidate_not_frozen",
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "shared_rpc_hop_ms": result,
    }
    OUTPUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
