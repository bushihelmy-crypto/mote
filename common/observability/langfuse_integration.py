"""Centralized Langfuse integration — code-noninvasive, default-off.

All langfuse imports are lazy (inside functions) so the framework runs without
langfuse installed when tracing is disabled. The public surface is three thin
helpers used at the integration points:

- ``make_async_openai(**kwargs)``: drop-in client factory. Enabled -> the
  langfuse-instrumented ``langfuse.openai.AsyncOpenAI``; otherwise the native
  ``openai.AsyncOpenAI``.
- ``maybe_trace(session_id, name, **attrs)``: contextmanager creating a root
  span for one role run with session propagation; ``nullcontext`` when disabled.
- ``maybe_span(name, **attrs)``: contextmanager for a child span (think/act/
  tool); ``nullcontext`` when disabled or ``trace_steps`` is off.

Activation is idempotent via ``init_langfuse``, called once from Config's
model_validator so env/client are ready before any LLM client is built.
"""
from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from metagpt.common.logs import logger

if TYPE_CHECKING:
    from metagpt.common.config.langfuse_config import LangfuseConfig

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


def steps_enabled() -> bool:
    return _ENABLED and _STEPS_ENABLED


def make_async_openai(**kwargs):
    """Return an AsyncOpenAI client; instrumented when Langfuse is enabled."""
    if _ENABLED:
        try:
            from langfuse.openai import AsyncOpenAI

            return AsyncOpenAI(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Langfuse openai drop-in unavailable ({e!r}); using native AsyncOpenAI.")

    from openai import AsyncOpenAI

    return AsyncOpenAI(**kwargs)


@contextmanager
def maybe_trace(session_id: str, name: str, **attrs):
    """Root span for a single role run, with session propagation.

    Zero-cost ``nullcontext`` when tracing is disabled.
    """
    if not _ENABLED:
        with nullcontext():
            yield
        return

    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(as_type="span", name=name) as span:
        try:
            client.update_current_trace(session_id=session_id)
        except Exception:  # noqa: BLE001 - propagation is best-effort
            pass
        if attrs:
            try:
                span.update(metadata=attrs)
            except Exception:  # noqa: BLE001
                pass
        yield


@contextmanager
def maybe_span(name: str, **attrs):
    """Child span for think/act/tool steps.

    Zero-cost ``nullcontext`` when tracing or step-tracing is disabled.
    """
    if not (_ENABLED and _STEPS_ENABLED):
        with nullcontext():
            yield
        return

    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(as_type="span", name=name) as span:
        if attrs:
            try:
                span.update(input=attrs)
            except Exception:  # noqa: BLE001
                pass
        yield
