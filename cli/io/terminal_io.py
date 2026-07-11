#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TerminalPort`` — the terminal's :class:`InteractivePort` (§2.5 / §8 split).

This is the input third of the old ``Repl`` god object, carved out per §8: the
``_setup_stdin`` / ``_on_sigint`` (two-stage) / ``_read_line`` / ``_console_ask``
machinery, with **all rendering and all ``context.messages`` reading removed**.
The port owns only inbound concerns:

* a cancellable, event-loop-integrated stdin reader (``asyncio.StreamReader``);
* the two-stage Ctrl+C state machine (mid-turn → interrupt callback; idle →
  double-press-to-exit) — an armed-flag state machine (NOT a wall-clock window:
  the arm persists until the user submits input, so a deliberate second press
  always exits), aligned with codex / claude-code;
* ``read_turn()`` (one turn per non-empty line; ``None`` on EOF / exit);
* ``ask()`` routing an ``AskUserQuestion`` to stdin, keeping a SINGLE reader so
  a foreground ask and a parked main-loop read never race for the same line.

Rendering moved to :class:`TerminalConsumer`; assistant replies now flow via
``MessageAppendedEvent`` → projector → consumer, so this port never reads the
role's context (the cut privileged path, §0.2). It surfaces only its own local
terminal chrome (the prompt + SIGINT notices) directly to ``out``.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any, Callable, Optional

# claude-code-style raw-mode menu decoration. The port writes raw strings to a
# plain stream (never imports ``rich``), so it reuses the rich-free truecolor
# ``ansi_fg`` (brand orange, mirrors ``Palette.BRAND``) plus a dim (bright-black)
# wrap for inactive numbers/hints — matching the consumer's ``❯`` figure so a
# selection menu reads the same brand as the rest of the terminal host. The pure
# row/redraw string builders live in ``terminal_menu`` (no ``self``, no I/O); the
# port keeps only the stateful reader + raw termios concerns.
from mote.cli.consumers.render.palette import WARN
from mote.cli.consumers.terminal.style import ansi_fg
from mote.cli.io.terminal_menu import (
    _AMBER_RGB,
    _RULE_WIDTH,
    _dim,
    _menu_lines,
    _redraw_menu,
    _redraw_option_lines,
    _render_option_lines,
)

# After a lone ESC byte we wait this long for the rest of a CSI arrow sequence
# (``ESC [ A/B``). A real arrow key sends all three bytes back-to-back, so a short
# window distinguishes it from a deliberate bare-Esc (cancel) without a laggy feel.
_CSI_TIMEOUT = 0.05


class TerminalPort:
    """Conversational stdin port: cancellable reads + two-stage Ctrl+C.

    Decoupling hooks (all optional, injected by the driver):

    * ``on_interrupt()`` — invoked when Ctrl+C arrives **mid-turn**; the driver
      wires it to ``control.interrupt(agent_id)``. The port stays agnostic to
      what "interrupt" means downstream.
    * ``is_turn_running()`` — lets the SIGINT handler decide mid-turn (interrupt)
      vs idle (double-press exit). Defaults to "never running".
    * ``get_input_reader()`` — test seam: supply a fake reader instead of stdin.
    """

    def __init__(
        self,
        *,
        prompt: Optional[str] = None,
        banner: Optional[str] = None,
        out=None,
        on_interrupt: Optional[Callable[[], Any]] = None,
        is_turn_running: Optional[Callable[[], bool]] = None,
        get_input_reader: Optional[Callable[[], Any]] = None,
        on_steer: Optional[Callable[[str], Any]] = None,
    ):
        # Default to the shared terminal look (orange ``❯`` prompt + masthead);
        # the style module is rich-free so importing it keeps the port's plain
        # stream contract. ``prompt``/``banner`` overrides are the test seam.
        from mote.cli.consumers.terminal.style import PROMPT, render_banner

        self._prompt = prompt if prompt is not None else PROMPT
        self._banner = banner if banner is not None else render_banner()
        self._out = out if out is not None else sys.stdout
        self._on_interrupt = on_interrupt
        self._is_turn_running = is_turn_running or (lambda: False)
        self._get_input_reader = get_input_reader
        # Driver-wired steering hook (mirror of ``_on_interrupt``): a submitted
        # steer is forwarded here for the driver to fold into the next turn.
        self._on_steer = on_steer

        self._reader: Any = None
        self._read_task: Optional[asyncio.Task] = None
        # Parked when ``ask`` runs while the main loop already holds a pending
        # ``readline()`` — the loop routes the next line to this future instead
        # of consuming it as new turn input (single-reader invariant).
        self._ask_waiter: Optional[asyncio.Future] = None
        # Idle Ctrl+C exit-arm: the first idle press arms it (and warns); the next
        # *consecutive* press exits. Reset when the user submits input (NOT by a
        # wall-clock timer) so a deliberate second press always exits, however
        # long the user takes to press it (aligned with claude-code / IPython).
        self._sigint_armed = False
        self._should_exit = False
        # An interrupted turn may stage its prompt here so the next ``read_turn``
        # shows it (line-buffered ttys can't pre-fill editable text).
        self._restored_input: Optional[str] = None
        self._sigint_installed = False
        self._idle_poll_interval = 0.1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Set up the stdin reader and install the SIGINT handler (best-effort)."""
        if self._banner:
            self._write(self._banner)
        await self._setup_stdin()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, self._on_sigint)
            self._sigint_installed = True
        except (NotImplementedError, RuntimeError):
            pass  # platform without signal-handler support — degrade gracefully

    async def aclose(self) -> None:
        """Remove the SIGINT handler. The stdin pipe is left to the loop."""
        if self._sigint_installed:
            try:
                asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
            except Exception:  # noqa: BLE001 — teardown must never crash
                pass
            self._sigint_installed = False

    async def _setup_stdin(self) -> None:
        if self._get_input_reader is not None:
            self._reader = self._get_input_reader()
            return
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        self._reader = reader

    # ------------------------------------------------------------------
    # Local terminal chrome (prompt + notices) — direct to out
    # ------------------------------------------------------------------
    def _write(self, text: str) -> None:
        try:
            self._out.write(text)
            self._out.flush()
        except Exception:  # noqa: BLE001 — never let console I/O crash the loop
            pass

    def _reprompt(self) -> None:
        self._write(self._prompt)

    # ------------------------------------------------------------------
    # SIGINT: two-stage state machine (aligned with cc)
    # ------------------------------------------------------------------
    def _on_sigint(self) -> None:
        if self._is_turn_running():
            # Mid-turn: abort the in-flight turn and return to the prompt. Disarm
            # any pending idle exit so returning to the prompt starts fresh.
            self._write("\n^C  interrupting current turn\u2026\n")
            self._sigint_armed = False
            if self._on_interrupt is not None:
                result = self._on_interrupt()
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
        elif self._sigint_armed:
            # Idle prompt, second consecutive press → exit (no timing window; the
            # arm was cleared by any intervening input, so reaching here means the
            # user pressed Ctrl+C twice with nothing typed between).
            self._should_exit = True
            if self._read_task is not None:
                self._read_task.cancel()
        else:
            # Idle prompt, first press → arm + warn; the next press exits.
            self._sigint_armed = True
            self._write("\n(Press Ctrl-C again to exit)\n")
            self._reprompt()

    # ------------------------------------------------------------------
    # InteractivePort.read_turn — one cancellable line per turn
    # ------------------------------------------------------------------
    async def read_turn(self) -> Optional[str]:
        """Read the next turn's input line, or ``None`` on EOF / exit.

        A bare Enter on an interrupted-and-restored prompt resends the staged
        text verbatim (line-buffered ttys can't offer editable pre-fill).
        """
        if self._should_exit or self._reader is None:
            return None
        restored = self._restored_input
        self._restored_input = None
        if restored:
            self._write("(interrupted — press Enter to resend, or type a new message)\n" f"  {restored}\n")
        self._reprompt()
        self._read_task = asyncio.ensure_future(self._reader.readline())
        try:
            data = await self._wait_read_or_route_ask()
        except asyncio.CancelledError:
            return None  # idle exit (double Ctrl+C)
        finally:
            self._read_task = None
        if not data:  # EOF (Ctrl+D)
            self._should_exit = True
            return None
        # The user submitted input — clear any pending Ctrl+C exit-arm so a later
        # lone press warns afresh instead of exiting on the first press.
        self._sigint_armed = False
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        line = data.rstrip("\n")
        if restored and not line.strip():
            return restored  # bare Enter resends the interrupted prompt
        return line

    async def _wait_read_or_route_ask(self) -> Any:
        """Await stdin; if an ``ask`` is parked, hand it the line and keep reading.

        The SOLE reader of ``self._reader`` while parked. A turn triggered while
        the loop waits here (e.g. a background-task ``AskUserQuestion``) parks
        ``_ask_waiter``; the next line goes to that waiter instead of being
        returned as new turn input — otherwise two concurrent ``readline()``
        calls would race for one line and the answer could be stolen.
        """
        while True:
            # A read task is always in flight while this loop runs; capture it into
            # a local so the .result() access below is narrowed off the Optional.
            read_task = self._read_task
            assert read_task is not None, "readline loop entered without an active read task"
            done, _ = await asyncio.wait({read_task}, timeout=self._idle_poll_interval)
            if read_task in done:
                data = read_task.result()
                waiter = self._ask_waiter
                if waiter is not None and not waiter.done():
                    waiter.set_result(data)
                    self._ask_waiter = None
                    self._read_task = asyncio.ensure_future(self._reader.readline())
                    continue
                return data

    # ------------------------------------------------------------------
    # InputPort.ask — AskUserQuestion → stdin (single-reader safe)
    # ------------------------------------------------------------------
    async def ask(self, ctx: Any, question: str, options: Optional[list] = None, multi: bool = False) -> str:
        """Ask *question* and return the answer, keeping the single-reader invariant.

        Pure free-text is the public contract (§7); the private ``options`` /
        ``multi`` kwargs remain only for the ``PortHumanChannel`` degrade path
        (a port that predates ``ask_questions``). With ``options`` it drives the
        same navigable menu; without them, the original single-line free-text
        prompt. Structured multi-choice goes through ``ask_questions``.
        """
        if options:
            labels = [str(o) for o in options]
            selected, free = await self._select_terminal(question, labels, multi)
            if free:
                return free
            return ", ".join(selected)
        self._write(f"\n{question}\n")
        self._reprompt()
        return await self._read_line()

    async def ask_questions(self, ctx: Any, questions: Any) -> Any:
        """Structured multiple-choice round-trip; mirrors ``decide_approval``.

        Each question reuses the navigable menu (options + "Other" free text +
        multi). The "Other" branch's raw string becomes ``free_text`` verbatim —
        multi-line or numeric, with zero reverse-parsing (this is the real fix).
        """
        from mote.common.schema import AskUserQuestionAnswer, AskUserQuestionAnswers

        out = []
        multiq = len(questions) > 1
        for q in questions:
            labels = [o.label for o in q.options]
            header = f"[{q.header}] {q.question}" if multiq else q.question
            selected, free = await self._select_structured(header, labels, q.multiSelect)
            out.append(AskUserQuestionAnswer(header=q.header, question=q.question, selected=selected, free_text=free))
        return AskUserQuestionAnswers(answers=out)

    async def _select_terminal(self, question: str, labels: list, multi: bool) -> tuple:
        """Drive the interactive or typed select, returning ``(selected, free)``."""
        if self._can_interactive_select():
            try:
                return await self._select_structured(question, labels, multi)
            except Exception:  # noqa: BLE001 — any tty/termios failure → typed path
                pass
        return await self._typed_select(question, labels)

    async def _typed_select(self, question: str, labels: list) -> tuple:
        """Non-interactive fallback: numbered options + one typed line.

        Returns ``(selected_labels, free_text)`` — a bare option number resolves
        to that label; anything else (including a digit outside range or free
        text) becomes ``free_text`` verbatim.
        """
        self._write(f"\n{question}\n")
        for i, label in enumerate(labels, start=1):
            self._write(f"  {i}. {label}\n")
        self._write(f"  {len(labels) + 1}. Other (type your own answer)\n")
        self._reprompt()
        answer = (await self._read_line()).strip()
        if answer.isdigit():
            idx = int(answer)
            if 1 <= idx <= len(labels):
                return [labels[idx - 1]], ""
        return [], answer

    async def _read_line(self) -> str:
        """Read one answer line while preserving the single-reader invariant.

        * Foreground turn: the main loop is blocked awaiting quiescence with no
          pending read, so this owns the reader and reads directly.
        * Parked main loop: a pending ``readline()`` exists; starting a second
          would race it. Park ``_ask_waiter`` so ``read_turn`` routes the next
          line here.

        Shared by ``ask`` and ``decide_approval`` so an interactive question and
        an approval prompt obey the same parked-waiter discipline.
        """
        if self._reader is None:
            return ""
        if self._read_task is not None and not self._read_task.done():
            waiter: asyncio.Future = asyncio.get_event_loop().create_future()
            self._ask_waiter = waiter
            try:
                data = await waiter
            finally:
                if self._ask_waiter is waiter:
                    self._ask_waiter = None
        else:
            data = await self._reader.readline()
        if not data:
            return ""
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        return data.rstrip("\n")

    # ------------------------------------------------------------------
    # ask — interactive select + free-text combo (claude-code parity)
    # ------------------------------------------------------------------
    _OTHER_LABEL = "Other (type your own answer)"

    async def _select_structured(self, question: str, labels: list, multi: bool) -> tuple:
        """Render a navigable menu (options + "Other"), return ``(selected, free)``.

        Single-select: Enter (or a digit) on a real option returns ``([label], "")``;
        on "Other" it drops to a free-text prompt → ``([], text)``. Multi-select:
        Space / digit toggle checkboxes, Enter confirms the set → ``(labels, "")``;
        "Other" still routes to free text. Esc / Ctrl+C / EOF return ``([], "")``.

        The "Other" free text is returned verbatim (no digit→label mapping), so a
        numeric or multi-line answer survives intact — the real fix.
        """
        entries = list(labels) + [self._OTHER_LABEL]
        other_index = len(entries) - 1
        # Light claude-code frame: a brand-orange top rule + bold question, then
        # the navigable rows, then a dim keyboard-hint footer.
        self._write("\n" + ansi_fg("\u2500" * _RULE_WIDTH) + "\n")
        self._write(f"{ansi_fg(question, bold=True)}\n")
        hint = "Space 选择 · Enter 确认 · Esc 取消" if multi else "↑↓ 选择 · Enter 确认 · Esc 取消"
        self._write(f"{_dim(hint)}\n\n")

        index = 0
        selected: set = set()
        cancelled = False
        restore = self._enter_raw()
        try:
            self._write("".join(_menu_lines(entries, index, selected, multi)))
            while True:
                key = await self._read_menu_key()
                done = False
                if key == "up":
                    index = (index - 1) % len(entries)
                elif key == "down":
                    index = (index + 1) % len(entries)
                elif key == "space" and multi and index != other_index:
                    selected.discard(index) if index in selected else selected.add(index)
                elif key == "enter":
                    done = True
                elif key == "cancel":
                    cancelled = True
                    done = True
                elif key.isdigit():
                    pos = int(key)
                    if 1 <= pos <= len(entries):
                        index = pos - 1
                        if multi and index != other_index:
                            selected.discard(index) if index in selected else selected.add(index)
                        else:
                            done = True
                    else:
                        continue
                else:
                    continue
                self._write(_redraw_menu(entries, index, selected, multi))
                if done:
                    break
        finally:
            restore()
        self._write("\n")
        if cancelled:
            return [], ""
        if index == other_index:
            return [], await self._prompt_free_text()
        if multi:
            picks = sorted(selected) if selected else [index]
            return [labels[i] for i in picks if i < len(labels)], ""
        return [labels[index]], ""

    async def _prompt_free_text(self) -> str:
        """Cooked-mode free-text entry (the "Other" branch of the menu)."""
        self._write("Type your answer:\n")
        self._reprompt()
        return await self._read_line()

    async def _read_csi_arrow(self) -> str:
        """After a lone ESC, resolve a CSI arrow (``ESC [ A/B``) or bare-Esc cancel.

        A real arrow key sends ``ESC [ A`` (up) / ``ESC [ B`` (down) back-to-back;
        a lone ESC that never completes within :data:`_CSI_TIMEOUT` is a deliberate
        bare Esc → ``cancel``. An incomplete/unknown sequence → ``other``.
        """
        try:
            b2 = await asyncio.wait_for(self._reader.read(1), _CSI_TIMEOUT)
        except asyncio.TimeoutError:
            return "cancel"  # bare Esc
        if b2 == b"[":
            try:
                b3 = await asyncio.wait_for(self._reader.read(1), _CSI_TIMEOUT)
            except asyncio.TimeoutError:
                return "other"
            if b3 == b"A":
                return "up"
            if b3 == b"B":
                return "down"
        return "other"

    async def _read_nav_key(self, extra_keys: Callable[[str], Optional[str]]) -> str:
        """Read one logical navigation key, shared by the menu + approval prompts.

        Handles the common vocabulary — arrows / ``j`` / ``k`` → ``up``/``down``;
        Enter → ``enter``; Esc / Ctrl+C / EOF → ``cancel`` — then defers the
        prompt-specific tail keys to *extra_keys* (a digit for the select menu,
        ``y``/``a``/``n``/``d`` for approval): it maps a lowercased char to its
        outcome or ``None`` to fall through to ``other`` (the caller re-loops).
        """
        b = await self._reader.read(1)
        if not b:
            return "cancel"  # EOF
        if b in (b"\r", b"\n"):
            return "enter"
        if b == b"\x03":  # Ctrl+C
            return "cancel"
        if b == b"\x1b":  # ESC — maybe a CSI arrow sequence
            return await self._read_csi_arrow()
        ch = b.decode(errors="ignore").lower()
        if ch == "k":
            return "up"
        if ch == "j":
            return "down"
        mapped = extra_keys(ch)
        if mapped is not None:
            return mapped
        return "other"

    async def _read_menu_key(self) -> str:
        """Read one logical key for the select menu.

        Arrows / ``j`` / ``k`` → ``up``/``down``; Enter → ``enter``; Space →
        ``space``; a digit returns itself; Esc / Ctrl+C / EOF → ``cancel``;
        anything else → ``other`` (the caller re-loops).
        """
        return await self._read_nav_key(lambda ch: "space" if ch == " " else (ch if ch.isdigit() else None))

    # Approval choices, in display order: ``(outcome, label, shortcut)``. The
    # shortcut key jumps to *and* selects the option (claude-code parity).
    _APPROVAL_OPTIONS = (
        ("accept", "Yes", "y"),
        ("always_allow", "Yes, and don\u2019t ask again for similar actions", "a"),
        ("reject", "No, and tell me what to do differently (esc)", "n"),
        ("always_deny", "No, and never allow this action", "d"),
    )

    async def decide_approval(self, ctx: Any, request: Any) -> Any:
        """Prompt for a gated action and map the choice to an ``ApprovalDecision``.

        On a real tty this renders a claude-code-style arrow-key menu: ↑/↓ (or
        ``k``/``j``) move the highlight, Enter selects, the ``y``/``a``/``n``/``d``
        shortcuts jump-and-select, Esc / Ctrl+C / EOF reject (the safe default).
        When stdin isn't an interactive terminal (tests, pipes) it degrades to the
        typed ``[y]/[n]/[a]/[d]`` prompt via the shared, parked-reader-safe
        ``_read_line`` so it never races the main-loop reader.
        """
        from mote.cli.contracts.view.events import ApprovalDecision

        action = getattr(request, "action", "") or getattr(request, "tool_name", "") or "action"
        risk = getattr(request, "risk", "medium")
        approval_id = getattr(request, "approval_id", "") or ""
        preview = getattr(request, "args_preview", "") or ""

        if self._can_interactive_select():
            try:
                outcome = await self._interactive_approval(action, risk, preview)
                return ApprovalDecision(approval_id=approval_id, outcome=outcome)
            except Exception:  # noqa: BLE001 — any tty/termios failure → typed path
                pass
        outcome = await self._typed_approval(action, risk, preview)
        return ApprovalDecision(approval_id=approval_id, outcome=outcome)

    # ------------------------------------------------------------------
    # Approval — interactive arrow-key menu (claude-code parity)
    # ------------------------------------------------------------------
    def _can_interactive_select(self) -> bool:
        """True when we can drive a raw-mode selectable menu on this stdin/stdout.

        A test seam (``_force_interactive``) forces it on with an injected reader.
        Otherwise it requires the real stdin (no injected reader), an interactive
        stdin+stdout tty, ``termios`` availability, and no parked main-loop read
        (a pending ``read_turn`` means a background-task approval — fall back to
        the parked-reader-safe typed path rather than steal raw bytes from it).
        """
        if getattr(self, "_force_interactive", False):
            return True
        if self._get_input_reader is not None:
            return False
        if self._read_task is not None and not self._read_task.done():
            return False
        try:
            import termios  # noqa: F401
        except ImportError:
            return False
        try:
            if not sys.stdin.isatty():
                return False
            out_isatty = getattr(self._out, "isatty", None)
            return bool(out_isatty and out_isatty())
        except Exception:  # noqa: BLE001
            return False

    async def _interactive_approval(self, action: str, risk: str, preview: str) -> str:
        """Render the menu, read navigation keys, return the chosen outcome."""
        options = self._APPROVAL_OPTIONS
        shortcuts = {sc: i for i, (_out, _lbl, sc) in enumerate(options)}
        # Header + preview render in cooked mode (plain ``\n``); the option block
        # below renders in raw mode and is the only region we redraw in place.
        # Light claude-code frame: an amber top rule, the bold amber gated-action
        # title, the dim command preview, then a neutral "proceed?" prompt.
        amber = lambda t, bold=False: ansi_fg(t, _AMBER_RGB, bold=bold)  # noqa: E731
        self._write("\n" + amber("\u2500" * _RULE_WIDTH) + "\n")
        self._write(amber(f"{WARN} approval required", bold=True) + _dim(f"  [{risk}]") + "\n")
        self._write(f"  {action}\n")
        if preview:
            self._write(f"{_dim(preview)}\n")
        self._write("\n")
        self._write("Do you want to proceed?\n")

        index = 0
        restore = self._enter_raw()
        try:
            self._write(_render_option_lines(options, index))
            while True:
                key = await self._read_key()
                done = False
                if key == "up":
                    index = (index - 1) % len(options)
                elif key == "down":
                    index = (index + 1) % len(options)
                elif key == "enter":
                    done = True
                elif key == "cancel":
                    index = shortcuts.get("n", 0)  # Esc / Ctrl+C / EOF → reject
                    done = True
                elif key in shortcuts:
                    index = shortcuts[key]
                    done = True
                else:
                    continue  # unmapped key — keep the menu up
                self._write(_redraw_option_lines(options, index))
                if done:
                    break
        finally:
            restore()
        self._write("\n")
        return options[index][0]

    def _enter_raw(self):
        """Put stdin into raw mode; return a restore callable (no-op on failure)."""
        try:
            import termios
            import tty

            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            tty.setraw(fd)

            def _restore() -> None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                except Exception:  # noqa: BLE001
                    pass

            return _restore
        except Exception:  # noqa: BLE001 — non-tty test / unsupported platform
            return lambda: None

    async def _read_key(self) -> str:
        """Read one logical key from the raw stdin stream.

        Maps arrows / ``j`` / ``k`` → ``up``/``down``; Enter → ``enter``; Esc,
        Ctrl+C and EOF → ``cancel``; the ``y``/``a``/``n``/``d`` shortcuts return
        themselves; anything else → ``other`` (the caller re-loops).
        """
        return await self._read_nav_key(lambda ch: ch if ch in ("y", "a", "n", "d") else None)

    async def _typed_approval(self, action: str, risk: str, preview: str) -> str:
        """Typed fallback: one line mapped to an outcome (non-tty / test path)."""
        self._write(f"\n\u26a0 approval required [{risk}]: {action}\n")
        if preview:
            self._write(f"{preview}\n")
        self._write("  [y]es / [n]o / [a]lways / [d]eny-always? ")
        answer = (await self._read_line()).strip().lower()
        return {
            "y": "accept",
            "yes": "accept",
            "a": "always_allow",
            "always": "always_allow",
            "d": "always_deny",
        }.get(answer, "reject")

    # ------------------------------------------------------------------
    # InputPort.signal_interrupt + exit / restore controls
    # ------------------------------------------------------------------
    def signal_interrupt(self, ctx: Any = None) -> None:
        """Programmatic interrupt (mirror of mid-turn Ctrl+C)."""
        if self._on_interrupt is not None:
            result = self._on_interrupt()
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    def submit_steer(self, ctx: Any = None, text: str = "") -> None:
        """Forward steering *text* to the driver for the next turn (§5.3).

        The public inbound entry point; the driver wires ``_on_steer`` to its
        queue. No mid-turn preemption — the terminal's own keyboard capture of
        in-flight steering is a later increment, so today this serves
        programmatic / cross-session steer submission.
        """
        if text and text.strip() and self._on_steer is not None:
            result = self._on_steer(text)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    def request_exit(self) -> None:
        """Signal the loop to exit and cancel any pending read (from /exit)."""
        self._should_exit = True
        if self._read_task is not None:
            self._read_task.cancel()

    def stage_restore(self, text: str) -> None:
        """Stage an interrupted turn's prompt for the next ``read_turn``."""
        self._restored_input = text

    @property
    def should_exit(self) -> bool:
        return self._should_exit


__all__ = ["TerminalPort"]
