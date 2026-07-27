#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The persistent bottom status line widget.

:class:`StatusBar` shows model / tokens / cost / ctx% plus a live shimmering
spinner (or ``✻ 思考中`` while reasoning) and a transient LLM-retry countdown.
It inherits plain ``Static`` (not :class:`SelectableStatic`) — chrome, not
transcript, so it needn't be mouse-selectable.
"""

from __future__ import annotations

import random
import time
from typing import Any, Optional

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from mote.contracts.text import format_token_count as _format_tok
from mote.product.cli.consumers.render.builders import USAGE_SEP, shimmer_text, sparkline
from mote.product.cli.consumers.textual.style import COMPACT, RETRY, STATUS_FG, Palette
from mote.product.i18n import keys as K
from mote.product.i18n import t


class StatusBar(Static):
    """Persistent bottom status line: model / tokens / cost / ctx% + spinner.

    Figures are pushed via :meth:`update_usage` (from ``UsageUpdated`` events);
    ``running`` toggles the braille spinner while a turn is in flight. Each is a
    Textual ``reactive`` so a change repaints the bar with no manual refresh.
    """

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $status-bg;
        color: $status-fg;
        padding: 0 1;
    }
    """

    _SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
    # Rotating activity-verb *keys* (shows "Working…/Fixing…/…"); one
    # is picked when a turn begins so the label doesn't flicker mid-turn, and the
    # key is translated at render time so ``/lang`` re-localises it live.
    _VERBS = K.STATUS_VERB_KEYS

    model: reactive[str] = reactive("")
    total_tokens: reactive[int] = reactive(0)
    cost_usd: reactive[Optional[float]] = reactive(None)
    context_pct: reactive[Optional[float]] = reactive(None)
    running: reactive[bool] = reactive(False)
    # Whether the model is currently *reasoning* (a ``ReasoningDelta`` stream). The
    # working label then reads ``✻ 思考中…`` instead of a generic verb (a
    # distinct "Thinking…" state), still with the live elapsed counter.
    thinking: reactive[bool] = reactive(False)
    _frame: reactive[int] = reactive(0)
    # Transient LLM-retry countdown (the "Retrying in Ns…" line). ``retry_msg`` empty
    # => not retrying; ``retry_secs`` ticks down to 0. Cleared on any other event.
    retry_msg: reactive[str] = reactive("")
    retry_secs: reactive[float] = reactive(0.0)
    durability_msg: reactive[str] = reactive("")

    #: Keep the last N per-update token deltas for the trend sparkline.
    _TOK_HISTORY = 16

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # When the current turn started (monotonic secs) → the live "(Ns)" elapsed
        # counter; 0.0 when idle. The verb chosen for this turn (stable mid-turn).
        self._run_started = 0.0
        self._verb = self._VERBS[0]
        # Per-update token deltas → a ``▁▂▃▅█`` trend sparkline in the status line.
        self._tok_history: list[int] = []
        self._last_total = 0
        self._lagging_runtimes: dict[str, str] = {}

    def set_thinking(self, flag: bool) -> None:
        """Enter/leave the ``✻ 思考中`` reasoning state (driven by the app)."""
        self.thinking = flag

    def watch_running(self, running: bool) -> None:
        """Stamp the turn start + pick a fresh verb when a turn begins/ends.

        Reactive watcher: fires on every ``running`` flip. Starting a turn records
        the monotonic clock (for the elapsed counter) and rolls a new activity
        verb; ending it resets so an idle bar shows no stale "(Ns)".
        """
        if running:
            self._run_started = time.monotonic()
            self._verb = random.choice(self._VERBS)
        else:
            self._run_started = 0.0

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if self.retry_msg and self.retry_secs > 0:
            self.retry_secs = max(0.0, self.retry_secs - 0.1)
        # Advance the shimmer/spinner frame whenever a working label is shown
        # (a turn in flight OR a reasoning stream), else freeze it when idle.
        if self.running or self.thinking:
            self._frame += 1

    def set_retry(self, ev: Any) -> None:
        """Show the transient retry countdown (from a ``RetryStatus`` event)."""
        etype = getattr(ev, "error_type", "") or "error"
        failed = t(K.RETRY_FAILED, error_type=etype)
        attempt = t(
            K.RETRY_ATTEMPT,
            attempt=getattr(ev, "attempt", 0),
            total=getattr(ev, "max_attempts", 0),
        )
        self.retry_msg = f"{failed}· {attempt}"
        self.retry_secs = max(0.0, (getattr(ev, "delay_ms", 0.0) or 0.0) / 1000.0)

    def clear_retry(self) -> None:
        """Erase the retry countdown — any non-retry event resolves it."""
        if self.retry_msg or self.retry_secs:
            self.retry_msg = ""
            self.retry_secs = 0.0

    def update_usage(self, ev: Any) -> None:
        if getattr(ev, "model", None):
            self.model = ev.model
        if getattr(ev, "total_tokens", 0):
            self.total_tokens = ev.total_tokens
        elif getattr(ev, "input_tokens", 0) or getattr(ev, "output_tokens", 0):
            self.total_tokens = (ev.input_tokens or 0) + (ev.output_tokens or 0)
        # Record the growth since the last update as a sparkline sample so the bar
        # shows a per-turn token-burst trend, not just the running total.
        if self.total_tokens > self._last_total:
            self._tok_history.append(self.total_tokens - self._last_total)
            del self._tok_history[: -self._TOK_HISTORY]
            self._last_total = self.total_tokens
        if getattr(ev, "cost_usd", None) is not None:
            self.cost_usd = ev.cost_usd
        if getattr(ev, "context_pct", None) is not None:
            self.context_pct = ev.context_pct

    def update_runtime_durability(self, ev: Any) -> None:
        key = getattr(ev, "runtime_id", "") or (f"{getattr(ev, 'runtime_kind', '')}:{getattr(ev, 'alias', 'default')}")
        if getattr(ev, "state", "") == "lagging":
            readable = f"{getattr(ev, 'runtime_kind', '')}:" f"{getattr(ev, 'alias', 'default')}"
            current = int(getattr(ev, "current_revision", 0) or 0)
            recoverable = int(getattr(ev, "recoverable_revision", 0) or 0)
            self._lagging_runtimes[key] = f"{readable} checkpoint lagging: r{current}, recoverable r{recoverable}"
        else:
            self._lagging_runtimes.pop(key, None)
        self.durability_msg = next(iter(self._lagging_runtimes.values()), "")

    def render(self) -> Text:
        # The transient retry countdown takes over the whole bar while active —
        # it's the one thing the human must see, in warning yellow.
        if self.retry_msg:
            # Span-coloured to match the terminal host: amber glyph + attempt
            # count, brand-orange live countdown tail.
            text = Text()
            text.append(f"{RETRY} ", style=f"bold {Palette.WARNING}")
            text.append(self.retry_msg, style=Palette.WARNING)
            text.append(" · ", style=Palette.DIM)
            text.append(
                t(K.RETRY_COUNTDOWN, secs=int(self.retry_secs + 0.999)),
                style=f"bold {Palette.BRAND}",
            )
            return text
        if self.durability_msg:
            text = Text("⚠ ", style=f"bold {Palette.WARNING}")
            text.append(self.durability_msg, style=Palette.WARNING)
            return text
        # Each field is its own ``Text`` so the live "working" segment can carry
        # brand colour while the metrics stay dim; joined by the shared ` │ `
        # separator (the status-line rule).
        segments: list[Text] = []
        if self.running or self.thinking:
            segments.append(self._working_text())
        if self.model:
            segments.append(Text(str(self.model), style=Palette.DIM))
        if self.total_tokens:
            segments.append(Text(f"{self.total_tokens:,} tok", style=Palette.DIM))
        if len(self._tok_history) >= 2:
            segments.append(sparkline(self._tok_history))
        if self.cost_usd is not None:
            segments.append(Text(f"${self.cost_usd:.4f}", style=Palette.DIM))
        if self.context_pct is not None:
            segments.append(Text(f"ctx {self.context_pct * 100:.0f}%", style=Palette.DIM))
        # The ctrl+o affordance moved OFF the bar onto the selected tool block's
        # bottom-right corner (see ``FoldableRow``), so the bar shows only metrics
        # — falling back to a dim ``就绪`` idle marker when there's nothing to show.
        if not segments:
            idle = Text(t(K.STATUS_IDLE), style=f"bold {STATUS_FG}")
            idle.append(USAGE_SEP, style=Palette.DIM)
            idle.append(t(K.STATUS_IDLE_HINT), style=Palette.DIM)
            return idle
        out = Text()
        for i, seg in enumerate(segments):
            if i:
                out.append(USAGE_SEP, style=Palette.DIM)
            out.append_text(seg)
        return out

    def _working_text(self) -> Text:
        """The live ``⠋ 工作中… (12s · 3.4k tok)`` activity segment with a shimmer.

        The label is a shimmering (微光) sweep — a bright band moving across the
        spinner+verb text, brand→light each frame (a glimmer sweep)
        — with a dim ``(elapsed · tokens)`` tail so the human sees the turn is
        progressing. During a reasoning stream it reads ``✻ 思考中`` instead, the
        distinct "thinking" state. The band + elapsed advance as the ``_frame``
        reactive repaints the bar.
        """
        if self.thinking:
            label = f"{COMPACT} " + t(K.STATUS_THINKING)
        else:
            label = f"{self._SPINNER[self._frame % len(self._SPINNER)]} {t(self._verb)}"
        text = shimmer_text(label, self._frame)
        text.append("…", style=Palette.DIM)
        meta: list[str] = []
        if self._run_started:
            meta.append(f"{int(time.monotonic() - self._run_started)}s")
        if self.total_tokens:
            meta.append(f"{_format_tok(self.total_tokens)} tok")
        if meta:
            text.append(f" ({' · '.join(meta)})", style=Palette.DIM)
        return text


__all__ = ["StatusBar"]
