"""Centralized Langfuse activation — code-noninvasive, default-off.

All langfuse imports are lazy (inside functions) so the framework runs without
langfuse installed when tracing is disabled. This module now owns only
*activation*: it reads config, sets the ``LANGFUSE_*`` env vars, constructs the
client, and exposes the enabled / step-tracing flags.

Instrumentation moved onto the spine: spans are emitted by the framework-native
``span`` contextmanager (:mod:`metagpt.common.events.trace`) as
``SpanStart``/``SpanEnd`` events, and LLM generations as request/response/error
events. A backend-agnostic :class:`~metagpt.common.observability.tracing.TracingSubscriber`
rebuilds the trace tree from explicit IDs and drives a pluggable
:class:`~metagpt.common.observability.tracing.TracerBackend`
(:class:`~metagpt.common.observability.langfuse_backend.LangfuseBackend` today).

Activation is idempotent via ``init_langfuse``, called once from Config's
model_validator so env/client are ready before any LLM client is built.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from metagpt.common.logs import logger

if TYPE_CHECKING:
    from metagpt.common.config.config.langfuse_config import LangfuseConfig

_ENABLED: bool = False
_STEPS_ENABLED: bool = False


def init_langfuse(cfg: "LangfuseConfig") -> bool:
    """Idempotently activate Langfuse from config.

    Sets the LANGFUSE_* env vars and constructs the client when enabled and keys
    are present. On missing keys or import failure, warns and stays disabled.
    Returns the resulting enabled state. Once enabled, subsequent calls are
    no-ops (idempotent).
    """
    global _ENABLED, _STEPS_ENABLED

    if _ENABLED:
        return True

    if not cfg.enabled:
        return False

    if not (cfg.public_key and cfg.secret_key):
        logger.warning("Langfuse enabled but public_key/secret_key missing; tracing stays disabled.")
        return False

    os.environ["LANGFUSE_PUBLIC_KEY"] = cfg.public_key
    os.environ["LANGFUSE_SECRET_KEY"] = cfg.secret_key
    os.environ["LANGFUSE_HOST"] = cfg.host
    os.environ["LANGFUSE_SAMPLE_RATE"] = str(cfg.sample_rate)

    try:
        from langfuse import get_client

        get_client()
    except Exception as e:  # noqa: BLE001 - import or client init may fail in many ways
        logger.warning(f"Langfuse enabled but client init failed ({e!r}); tracing stays disabled.")
        return False

    _ENABLED = True
    _STEPS_ENABLED = cfg.trace_steps
    logger.info(f"Langfuse tracing enabled (host={cfg.host}, trace_steps={cfg.trace_steps}).")
    return True


def is_enabled() -> bool:
    return _ENABLED


def step_tracing_enabled() -> bool:
    """Whether per-step spans (think/act/tool) should be exported.

    Read by the bus wiring to seed :class:`TracingSubscriber.trace_steps`, which
    applies the knob at the exporter boundary (root span + generations always
    export; non-root step spans are skipped when this is off).
    """
    return _STEPS_ENABLED
