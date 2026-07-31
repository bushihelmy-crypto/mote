"""Generate the frozen Bifrost provider/operation parity manifest.

The generator intentionally reads implementation bodies instead of the broad Go
provider interface: unsupported stubs implement that interface too.  It is a
Gate 0 evidence tool and never participates in a production execution path.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

BASELINE_COMMIT = "ec1dd920619955415bd6d61ab9ecff71f170ee22"
PROVIDERS = (
    "anthropic",
    "azure",
    "bedrock",
    "bedrockmantle",
    "cerebras",
    "cohere",
    "deepseek",
    "elevenlabs",
    "fireworks",
    "gemini",
    "groq",
    "huggingface",
    "mistral",
    "nebius",
    "ollama",
    "openai",
    "opencode",
    "openrouter",
    "parasail",
    "perplexity",
    "replicate",
    "runware",
    "runway",
    "sarvam",
    "sgl",
    "vertex",
    "vllm",
    "wafer",
    "xai",
)

# Canonical operation id, Go method, execution taxonomy, wire unit, side effect.
OPERATIONS = (
    ("models.list", "ListModels", "unary_finite_attempt", "http_request", False),
    ("tokens.count", "CountTokens", "unary_finite_attempt", "http_request", False),
    ("compaction.create", "Compaction", "unary_finite_attempt", "http_request", False),
    ("text.complete", "TextCompletion", "unary_finite_attempt", "http_request", False),
    ("text.complete_stream", "TextCompletionStream", "unary_finite_attempt", "http_request", False),
    ("chat.complete", "ChatCompletion", "unary_finite_attempt", "http_request", False),
    ("chat.complete_stream", "ChatCompletionStream", "unary_finite_attempt", "http_request", False),
    ("responses.create", "Responses", "unary_finite_attempt", "http_request", False),
    ("responses.create_stream", "ResponsesStream", "unary_finite_attempt", "http_request", False),
    ("responses.retrieve", "ResponsesRetrieve", "durable_operation", "http_request", False),
    ("responses.retrieve_stream", "ResponsesRetrieveStream", "durable_operation", "http_request", False),
    ("responses.delete", "ResponsesDelete", "durable_operation", "http_request", True),
    ("responses.cancel", "ResponsesCancel", "durable_operation", "http_request", True),
    ("responses.input_items", "ResponsesInputItems", "durable_operation", "http_request", False),
    ("embedding.create", "Embedding", "unary_finite_attempt", "http_request", False),
    ("rerank.create", "Rerank", "unary_finite_attempt", "http_request", False),
    ("ocr.create", "OCR", "unary_finite_attempt", "http_request", False),
    ("speech.create", "Speech", "unary_finite_attempt", "http_request", False),
    ("speech.create_stream", "SpeechStream", "unary_finite_attempt", "http_request", False),
    ("transcription.create", "Transcription", "unary_finite_attempt", "http_request", False),
    ("transcription.create_stream", "TranscriptionStream", "unary_finite_attempt", "http_request", False),
    ("image.generate", "ImageGeneration", "unary_finite_attempt", "http_request", False),
    ("image.generate_stream", "ImageGenerationStream", "unary_finite_attempt", "http_request", False),
    ("image.edit", "ImageEdit", "unary_finite_attempt", "http_request", False),
    ("image.edit_stream", "ImageEditStream", "unary_finite_attempt", "http_request", False),
    ("image.variation", "ImageVariation", "unary_finite_attempt", "http_request", False),
    ("video.generate", "VideoGeneration", "durable_operation", "http_request", True),
    ("video.retrieve", "VideoRetrieve", "durable_operation", "http_request", False),
    ("video.download", "VideoDownload", "artifact_transfer", "http_range_request", False),
    ("video.delete", "VideoDelete", "durable_operation", "http_request", True),
    ("video.list", "VideoList", "durable_operation", "http_request", False),
    ("video.remix", "VideoRemix", "durable_operation", "http_request", True),
    ("batch.create", "BatchCreate", "durable_operation", "http_request", True),
    ("batch.list", "BatchList", "durable_operation", "http_request", False),
    ("batch.retrieve", "BatchRetrieve", "durable_operation", "http_request", False),
    ("batch.cancel", "BatchCancel", "durable_operation", "http_request", True),
    ("batch.delete", "BatchDelete", "durable_operation", "http_request", True),
    ("batch.results", "BatchResults", "artifact_transfer", "http_range_request", False),
    ("file.upload", "FileUpload", "artifact_transfer", "http_part_request", True),
    ("file.list", "FileList", "durable_operation", "http_request", False),
    ("file.retrieve", "FileRetrieve", "durable_operation", "http_request", False),
    ("file.delete", "FileDelete", "durable_operation", "http_request", True),
    ("file.content", "FileContent", "artifact_transfer", "http_range_request", False),
    ("cached_content.create", "CachedContentCreate", "durable_operation", "http_request", True),
    ("cached_content.list", "CachedContentList", "durable_operation", "http_request", False),
    ("cached_content.retrieve", "CachedContentRetrieve", "durable_operation", "http_request", False),
    ("cached_content.update", "CachedContentUpdate", "durable_operation", "http_request", True),
    ("cached_content.delete", "CachedContentDelete", "durable_operation", "http_request", True),
    ("container.create", "ContainerCreate", "durable_operation", "http_request", True),
    ("container.list", "ContainerList", "durable_operation", "http_request", False),
    ("container.retrieve", "ContainerRetrieve", "durable_operation", "http_request", False),
    ("container.delete", "ContainerDelete", "durable_operation", "http_request", True),
    ("container_file.create", "ContainerFileCreate", "artifact_transfer", "http_part_request", True),
    ("container_file.list", "ContainerFileList", "durable_operation", "http_request", False),
    ("container_file.retrieve", "ContainerFileRetrieve", "durable_operation", "http_request", False),
    ("container_file.content", "ContainerFileContent", "artifact_transfer", "http_range_request", False),
    ("container_file.delete", "ContainerFileDelete", "durable_operation", "http_request", True),
    ("passthrough.buffered", "Passthrough", "unary_finite_attempt", "http_request", False),
    ("passthrough.streaming", "PassthroughStream", "unary_finite_attempt", "http_request", False),
)

UNSUPPORTED = "NewUnsupportedOperationError"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def _method_body(text: str, method: str) -> tuple[str, int] | None:
    match = re.search(rf"^func \([^\n]+\) {re.escape(method)}\(", text, re.MULTILINE)
    if match is None:
        return None
    opening = text.find("{", match.end())
    if opening < 0:
        raise ValueError(f"method {method} has no body")
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index], text.count("\n", 0, match.start()) + 1
    raise ValueError(f"unterminated method {method}")


def _provider_sources(root: Path, provider: str) -> list[Path]:
    directory = root / "core" / "providers" / provider
    return sorted(path for path in directory.rglob("*.go") if not path.name.endswith("_test.go"))


def _find_implementation(root: Path, provider: str, method: str) -> tuple[Path, str, int] | None:
    matches: list[tuple[Path, str, int]] = []
    for path in _provider_sources(root, provider):
        body = _method_body(path.read_text(encoding="utf-8"), method)
        if body is not None:
            matches.append((path, body[0], body[1]))
    if len(matches) > 1:
        raise ValueError(f"{provider}.{method}: expected at most one implementation, found {len(matches)}")
    return matches[0] if matches else None


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _translation_profile(provider: str) -> str:
    if provider in {"anthropic", "bedrock", "bedrockmantle"}:
        return "anthropic_messages_v1"
    if provider in {"gemini", "vertex"}:
        return "google_generate_content_v1"
    return "openai_family_v1"


def _cell(root: Path, provider: str, operation: tuple[str, str, str, str, bool]) -> dict[str, Any]:
    operation_id, method, taxonomy, wire_unit, side_effect = operation
    implementation = _find_implementation(root, provider, method)
    optional_absence = (
        implementation is None and method.startswith("Responses") and method not in {"Responses", "ResponsesStream"}
    )
    if implementation is None and not optional_absence:
        raise ValueError(f"{provider}.{method}: required Provider method is absent")
    if implementation is None:
        source = root / "core" / "schemas" / "provider.go"
        body = ""
        line = 736
    else:
        source, body, line = implementation
    unsupported = optional_absence or UNSUPPORTED in body
    status = "unsupported"
    if not unsupported:
        status = "provider_managed" if taxonomy == "durable_operation" else "supported"
    relative = source.relative_to(root).as_posix()
    streaming = operation_id.endswith("_stream") or operation_id == "passthrough.streaming"
    return {
        "provider": provider,
        "operation": operation_id,
        "status": status,
        "release_scope": ["current_embedded", "current_shared"],
        "evidence": {
            "source": [f"{relative}:{line}"],
            "source_digest": _digest(source),
            "derivation": (
                "optional_interface_absent"
                if optional_absence
                else "explicit_unsupported_stub"
                if unsupported
                else "concrete_method_body"
            ),
        },
        "mote_contract_tests": [f"parity.{provider}.{operation_id}"],
        "auth": {"credential_class": f"{provider}_credential", "secret_in_manifest": False},
        "streaming": streaming,
        "side_effect": side_effect,
        "idempotency": "provider_key_when_available" if side_effect else "attempt_or_command_id",
        "execution": {
            "taxonomy": taxonomy,
            "logical_owner": {
                "unary_finite_attempt": "RuntimeModelGateway",
                "durable_operation": "RuntimeServiceGateway",
                "long_lived_session": "SessionGateway",
                "artifact_transfer": "ArtifactTransferWorkflow",
            }[taxonomy],
            "wire_unit": wire_unit,
            "commit_boundary": "send_committed_before_irreversible_wire",
            "fallback_boundary": "before_external_commit_only",
            "poll_owner": "RuntimeServiceGateway" if taxonomy == "durable_operation" else "none",
            "reconcile": "provider_query_or_receipt" if taxonomy == "durable_operation" else "attempt_receipt",
            "generation_pin": "until_terminal_or_reconciliation",
            "usage_settlement": "receipt_idempotent",
            "terminal_or_in_doubt_oracle": "ordered_lifecycle_and_receipt",
        },
        "translation": {
            "profile": _translation_profile(provider),
            "preserve": ["provider_extensions", "reasoning", "tools", "cache_control"],
            "drop": [],
            "transform": ["canonical_identity", "usage", "failure"],
            "round_trip": ["reasoning", "tools", "cache_control"],
            "unknown_stream_event": "preserve_opaque_and_do_not_commit",
            "forbidden_headers": ["authorization", "proxy-authorization", "connection"],
            "upstream_headers": "allowlist_only",
            "client_profiles": ["canonical_v1"],
        },
        "catalog": {"provenance": "frozen_bifrost_source", "freshness": "generation_pinned", "signature": "required"},
        "reasoning_replay": "required" if operation_id.startswith(("chat.", "responses.")) else "not_applicable",
        "fixture_digest": None,
    }


def generate(root: Path) -> dict[str, Any]:
    head = _git(root, "rev-parse", "HEAD")
    if head != BASELINE_COMMIT:
        raise ValueError(f"expected Bifrost {BASELINE_COMMIT}, got {head}")
    cells = [_cell(root, provider, operation) for provider in PROVIDERS for operation in OPERATIONS]
    return {
        "schema_version": 1,
        "baseline": {"repository": "Bifrost", "commit": head, "dirty": bool(_git(root, "status", "--porcelain"))},
        "generation": {"method": "source_scan_v1", "unsupported_marker": UNSUPPORTED},
        "providers": list(PROVIDERS),
        "operations": [operation[0] for operation in OPERATIONS],
        "cells": cells,
        "gate_status": "evidence_inventory_generated",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bifrost-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = generate(args.bifrost_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")


if __name__ == "__main__":
    main()
