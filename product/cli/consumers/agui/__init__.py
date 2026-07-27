#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AG-UI transport — the SSE host for CopilotKit v2 / any AG-UI client.

The output half (:class:`AguiConsumer`) folds mote's neutral ``ViewEvent`` stream
into AG-UI wire events (via the pure :mod:`mote.product.cli.consumers._wire.agui` mapper)
and writes them as SSE ``data:`` frames; the input half (:class:`AguiPort`)
carries the per-request user turn upstream. :func:`create_app` mounts them on an
``aiohttp`` app that drives one turn per ``POST /agent/{id}/run`` against a
resident session pulled from a shared :class:`SessionRegistry`.

This is a pure *transport* layer: every wire-shape decision lives in the
``_wire/agui`` mapper (unit-testable without a socket), and every multi-session
concern lives in ``cli/serving`` (``SessionRegistry`` + ``ConnectionScope``). The
package here just moves bytes.
"""

from __future__ import annotations

from mote.product.cli.consumers.agui.consumer import AguiConsumer, build_agui_consumer
from mote.product.cli.consumers.agui.port import AguiPort

__all__ = ["AguiConsumer", "build_agui_consumer", "AguiPort"]
