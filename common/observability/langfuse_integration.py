"""Centralized Langfuse integration — code-noninvasive, default-off.

All langfuse imports are lazy (inside functions) so the framework runs without
langfuse installed when tracing is disabled. The public surface is thin helpers
used at the integration points:

- ``maybe_trace(session_id, name, **attrs)``: contextmanager creating a root
  span for one role run with session propagation; ``nullcontext`` when disabled.
- ``maybe_span(name, **attrs)``: contextmanager for a child span (think/act/
  tool); ``nullcontext`` when disabled or ``trace_steps`` is off.
- ``maybe_generation(model, messages, **attrs)``: contextmanager for a single
  LLM generation (the proper Langfuse observation type for model calls).
  Records model, input messages, and — on exit — output and usage. Used by
  ``BaseLLM`` as a unified instrumentation hook for all providers.

Activation is idempotent via ``init_langfuse``, called once from Config's
model_validator so env/client are ready before any LLM client is built.
"""
from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
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


def steps_enabled() -> bool:
    return _ENABLED and _STEPS_ENABLED


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


@contextmanager
def maybe_generation(model: str, input_messages: list | None = None, **attrs):
    """LLM generation observation — the unified instrumentation hook for BaseLLM.

    Creates a Langfuse ``generation`` observation (the correct type for model
    calls, recording model name, input, output, and usage). The yielded object
    exposes a ``.update()`` method that the caller uses to report output and
    usage after the completion returns. When tracing is disabled the yielded
    object is a no-op stub.

    Usage in BaseLLM::

        with maybe_generation(self.model, messages) as gen:
            rsp = await self._achat_completion(messages, ...)
            gen.update(output=..., usage=...)
    """

    class _NoOp:
        """Stub yielded when tracing is disabled — all methods are silent no-ops."""

        def update(self, **_kwargs):
            pass

    if not _ENABLED:
        yield _NoOp()
        return

    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(as_type="generation", name=f"llm:{model}") as gen:
        try:
            gen.update(model=model, input=input_messages, metadata=attrs or None)
        except Exception:  # noqa: BLE001
            pass
        yield gen
