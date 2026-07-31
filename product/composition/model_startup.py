"""Initial atomic application-composition installation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mote.contracts.runtime.application import ExpectedEmpty, SourceRevision
from mote.product.composition.model_application import AtomicApplicationComposition
from mote.product.composition.model_builder import build_application_candidate
from mote.product.config.diagnostics import _is_secret
from mote.product.config.schema import Config
from mote.product.models.registry import LLMProviderRegistry


def _redacted_source(value, path: str = ""):
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if _is_secret(f"{path}.{key}".strip("."))
            else _redacted_source(item, f"{path}.{key}".strip("."))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redacted_source(item, path) for item in value]
    return value


def source_revision(config: Config) -> SourceRevision:
    public = _redacted_source(config.model_dump(mode="json"))
    payload = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return SourceRevision(hashlib.sha256(payload).hexdigest())


async def install_initial_application_composition(
    config: Config,
    *,
    providers: LLMProviderRegistry,
    oauth_root: Path,
    cost_tracker=None,
    admission_controller=None,
    model_call_journal=None,
) -> AtomicApplicationComposition:
    composition = AtomicApplicationComposition()
    revision = source_revision(config)
    sequence = composition.accept_reload_request(revision)
    candidate = await build_application_candidate(
        config,
        reload_sequence=sequence,
        source_revision=revision,
        providers=providers,
        oauth_root=oauth_root,
        cost_tracker=cost_tracker,
        admission_controller=admission_controller,
        model_call_journal=model_call_journal,
    )
    token = composition.issue_activation_token()
    await composition.activate(candidate, token, ExpectedEmpty())
    return composition


__all__ = ["install_initial_application_composition", "source_revision"]
