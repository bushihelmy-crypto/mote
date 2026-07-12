#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the leaf consumers + the human-channel env adapter.

``StructuredConsumer`` is the §8.1 phase③ litmus test — it serializes every
``ViewEvent`` kind identically to one JSON line via ``on_unhandled`` (it defines
no per-kind methods), so a delta and a tool result both round-trip as JSON.
``PlainTerminalConsumer`` is the rich-less fallback that only ever renders whole
blocks. ``PortHumanChannel`` routes ``Role.ask_user`` to an ``InputPort.ask``;
everything else on the env surface is an inert no-op for the single-agent driver.
"""

from __future__ import annotations

import io
import json

import pytest

from mote.cli.consumers.structured.consumer import StructuredConsumer
from mote.cli.consumers.terminal.consumer import _HAS_RICH, PlainTerminalConsumer
from mote.cli.contracts.view import (
    ConversationCompacted,
    ErrorRaised,
    MessageBlockCompleted,
    MessageBlockDelta,
    Notice,
    RetryStatus,
    SystemReminder,
    ToolCallCompleted,
    ToolCallStarted,
)
from mote.cli.io.human_channel import PortHumanChannel
from mote.common.i18n import keys as K
from mote.common.i18n import t

# --------------------------------------------------------------------------
# StructuredConsumer — one JSON line per ViewEvent
# --------------------------------------------------------------------------


def test_structured_serializes_delta_via_sync_path():
    buf = io.StringIO()
    c = StructuredConsumer(out=buf)
    c.handle_sync(MessageBlockDelta(text="hi"))
    line = buf.getvalue().strip()
    payload = json.loads(line)
    assert payload == {"kind": "message_block_delta", "text": "hi"}


@pytest.mark.asyncio
async def test_structured_serializes_completed_via_async_path():
    buf = io.StringIO()
    c = StructuredConsumer(out=buf)
    await c.handle(MessageBlockCompleted(role="assistant", markdown="done", streamed=True))
    payload = json.loads(buf.getvalue().strip())
    assert payload["kind"] == "message_block_completed"
    assert payload["markdown"] == "done"
    assert payload["streamed"] is True


def test_structured_emits_one_line_per_event_in_order():
    buf = io.StringIO()
    c = StructuredConsumer(out=buf)
    c.handle_sync(Notice(text="a"))
    c.handle_sync(Notice(text="b"))
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert [p["text"] for p in lines] == ["a", "b"]


# --------------------------------------------------------------------------
# PlainTerminalConsumer — whole-block plain text
# --------------------------------------------------------------------------


def test_plain_prints_completed_block():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_message_block_completed(MessageBlockCompleted(markdown="hello there"))
    assert buf.getvalue() == "hello there\n"


def test_plain_skips_empty_completed_block():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_message_block_completed(MessageBlockCompleted(markdown="   "))
    assert buf.getvalue() == ""


def test_plain_renders_user_message_with_chevron():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_message_block_completed(MessageBlockCompleted(role="user", markdown="hi agent"))
    assert buf.getvalue() == "> hi agent\n"


def test_plain_skips_empty_user_message():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_message_block_completed(MessageBlockCompleted(role="user", markdown="  "))
    assert buf.getvalue() == ""


def test_plain_renders_tool_call_started_with_headline_and_body():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_tool_call_started(ToolCallStarted(tool_name="Write", headline="a.py", body="print(1)"))
    out = buf.getvalue()
    assert "[Write]  a.py" in out
    assert "print(1)" in out


def test_plain_renders_tool_completion_marks():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_tool_call_completed(ToolCallCompleted(ok=True, summary="ok"))
    c.on_tool_call_completed(ToolCallCompleted(ok=False, summary="boom"))
    out = buf.getvalue()
    assert "✓ ok" in out
    assert "✗ boom" in out


def test_plain_renders_notice_and_error():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_notice(Notice(text="just so you know"))
    c.on_error_raised(ErrorRaised(text="it failed"))
    out = buf.getvalue()
    assert "just so you know" in out
    assert "Error: it failed" in out


def test_plain_renders_system_reminder():
    # The injected turn-context summary prints with the ⚑ note glyph.
    from mote.cli.consumers.render.palette import NOTE

    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_system_reminder(SystemReminder(text="Git status · Files changed on disk"))
    out = buf.getvalue()
    assert NOTE in out
    assert "Git status · Files changed on disk" in out


def test_plain_renders_conversation_compacted():
    # A compaction boundary prints the ✻ marker + the retained-message count.
    from mote.cli.consumers.render.palette import COMPACT

    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_conversation_compacted(ConversationCompacted(summary="recap", message_count=7))
    out = buf.getvalue()
    assert COMPACT in out
    assert t(K.COMPACT_COMPACTED) in out
    assert t(K.COMPACT_KEPT, count=7) in out


def test_plain_conversation_compacted_omits_count_when_zero():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_conversation_compacted(ConversationCompacted(summary="recap"))
    out = buf.getvalue()
    assert t(K.COMPACT_COMPACTED) in out
    # count omitted → no parenthetical kept-count clause on the marker line.
    assert "(" not in out


def test_plain_fold_note_shows_hidden_line_count():
    # hidden_lines > 0 (no disk ref) → the FOLD_HIDDEN_LINES count note.
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_tool_call_completed(ToolCallCompleted(ok=True, summary="ok", content_truncated=True, hidden_lines=12))
    out = buf.getvalue()
    assert t(K.FOLD_HIDDEN_LINES, count=12) in out


def test_plain_fold_note_shows_scissors_for_hard_truncation():
    # full_ref present → the ✂ hard-truncation marker + the disk reference.
    from mote.cli.consumers.render.palette import SCISSORS

    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_tool_call_completed(ToolCallCompleted(ok=True, summary="ok", content_truncated=True, full_ref="/tmp/out.txt"))
    out = buf.getvalue()
    assert SCISSORS in out
    assert t(K.FOLD_FULL_REF, ref="/tmp/out.txt") in out
    assert "/tmp/out.txt" in out


def test_plain_fold_note_generic_when_no_count_or_ref():
    # content_truncated but neither hidden_lines nor full_ref → generic note.
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_tool_call_completed(ToolCallCompleted(ok=True, summary="ok", content_truncated=True))
    out = buf.getvalue()
    assert t(K.FOLD_CONTENT) in out


def test_plain_no_fold_note_when_not_truncated():
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_tool_call_completed(ToolCallCompleted(ok=True, summary="ok", hidden_lines=5))
    out = buf.getvalue()
    # Not truncated → no fold note of any flavour (count / content / hard-ref).
    assert t(K.FOLD_HIDDEN_LINES, count=5) not in out
    assert t(K.FOLD_CONTENT) not in out


def test_plain_retry_status_is_silent():
    # No erasable region in plain mode → the transient retry must not print.
    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_retry_status(RetryStatus(attempt=1, max_attempts=6, delay_ms=2000.0))
    assert buf.getvalue() == ""


# --------------------------------------------------------------------------
# TerminalSurface (rich) — transient retry countdown that self-clears
#
# The scrolling host is now a ``SurfaceDriver`` (the shared reducer) wrapping a
# ``TerminalSurface`` (the rich primitives). Tests drive the driver's neutral
# dispatch entry (``on_unhandled`` folds every ViewEvent through the reducer) and
# inspect the surface's rich state — so the retry-clearing / grouping / thinking
# *timing* is asserted through the same machine both hosts share.
# --------------------------------------------------------------------------


def _rich_console(width: int = 120):
    from rich.console import Console

    return Console(file=io.StringIO(), force_terminal=True, width=width)


def _terminal_pair(console):
    """Build the ``(driver, surface)`` pair the rich terminal host ships as."""
    from mote.cli.consumers.terminal.surface import TerminalSurface
    from mote.cli.consumers.transcript import SurfaceDriver

    surface = TerminalSurface(console=console)
    return SurfaceDriver(surface), surface


def _live_plain(live) -> str:
    """Extract the plain text of a Live's renderable (Live may wrap it in a Group)."""
    from rich.console import Group

    r = live.renderable
    if isinstance(r, Group):
        r = r.renderables[0]
    return r.plain


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_retry_status_opens_transient_line():
    driver, surface = _terminal_pair(_rich_console())
    driver.on_unhandled(RetryStatus(attempt=3, max_attempts=6, delay_ms=2000.0, error_type="LLMOverloadedError"))
    assert surface._retry_live is not None
    text = _live_plain(surface._retry_live)
    assert "\u27f3" in text  # ⟳ retry glyph (not the ⚠ approval gate)
    assert t(K.RETRY_ATTEMPT, attempt=3, total=6) in text
    assert "LLMOverloadedError" in text
    assert t(K.RETRY_COUNTDOWN, secs=2) in text


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_retry_cleared_by_next_event():
    driver, surface = _terminal_pair(_rich_console())
    driver.on_unhandled(RetryStatus(attempt=1, max_attempts=6, delay_ms=1000.0))
    assert surface._retry_live is not None
    # Any subsequent event (here a notice) wipes the transient retry line.
    driver.on_unhandled(Notice(text="hello"))
    assert surface._retry_live is None


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_retry_cleared_by_stream_delta():
    driver, surface = _terminal_pair(_rich_console())
    driver.on_unhandled(RetryStatus(attempt=1, max_attempts=6, delay_ms=1000.0))
    assert surface._retry_live is not None
    # A streamed token means the retry succeeded — the countdown must vanish.
    driver.on_unhandled(MessageBlockDelta(text="hi"))
    assert surface._retry_live is None


# --------------------------------------------------------------------------
# TerminalSurface (rich) — converged grouping + thinking (new to the terminal)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_groups_read_search_into_one_summary_line():
    # A Read/Grep/Glob run coalesces into a single ``● 搜索 N · 读取 M`` line
    # (buffered until the run breaks) — the terminal now shares the Textual host's
    # collapse-read-search behaviour via the reducer.
    console = _rich_console()
    driver, _ = _terminal_pair(console)
    driver.on_unhandled(ToolCallStarted(tool_name="Read", headline="a.py", tool_use_id="t1"))
    driver.on_unhandled(ToolCallCompleted(tool_name="Read", ok=True, summary="", tool_use_id="t1"))
    driver.on_unhandled(ToolCallStarted(tool_name="Grep", headline="foo", tool_use_id="t2"))
    driver.on_unhandled(ToolCallCompleted(tool_name="Grep", ok=True, summary="", tool_use_id="t2"))
    # A non-transparent event breaks the run and flushes the one summary line.
    driver.on_unhandled(Notice(text="done"))
    out = console.file.getvalue()
    assert t(K.GROUP_READ, count=1) in out
    assert t(K.GROUP_SEARCH, count=1) in out


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_thinking_opens_and_clears_transient():
    # Reasoning tokens surface only as the transient ``✻ 思考中`` indicator; a
    # visible reply delta ends the thinking state (the reducer sequences it).
    from mote.cli.consumers.render.palette import COMPACT
    from mote.cli.contracts.view import ReasoningDelta

    console = _rich_console()
    driver, surface = _terminal_pair(console)
    driver.on_unhandled(ReasoningDelta(text="pondering"))
    assert surface._thinking_live is not None
    text = _live_plain(surface._thinking_live)
    assert COMPACT in text
    assert t(K.STATUS_THINKING) in text
    # The reasoning tokens themselves never enter the permanent scrollback.
    assert "pondering" not in console.file.getvalue()
    # A visible reply delta leaves the thinking state.
    driver.on_unhandled(MessageBlockDelta(text="answer"))
    assert surface._thinking_live is None


# --------------------------------------------------------------------------
# TerminalSurface (rich) — compaction boundary + tool-result fold hints
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_renders_conversation_compacted():
    from mote.cli.consumers.render.palette import COMPACT

    console = _rich_console()
    driver, _ = _terminal_pair(console)
    driver.on_unhandled(ConversationCompacted(summary="recap", message_count=4))
    out = console.file.getvalue()
    assert COMPACT in out
    assert t(K.COMPACT_COMPACTED) in out
    assert t(K.COMPACT_KEPT, count=4) in out


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_fold_note_hidden_lines():
    console = _rich_console()
    driver, _ = _terminal_pair(console)
    driver.on_unhandled(ToolCallCompleted(ok=True, summary="ok", content_truncated=True, hidden_lines=9))
    out = console.file.getvalue()
    assert t(K.FOLD_HIDDEN_LINES, count=9) in out


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_fold_note_hard_truncation_shows_scissors():
    from mote.cli.consumers.render.palette import SCISSORS

    console = _rich_console()
    driver, _ = _terminal_pair(console)
    driver.on_unhandled(ToolCallCompleted(ok=True, summary="ok", content_truncated=True, full_ref="/tmp/big.log"))
    out = console.file.getvalue()
    assert SCISSORS in out
    assert t(K.FOLD_FULL_REF, ref="/tmp/big.log") in out


# --------------------------------------------------------------------------
# TerminalSurface.render_media — native-protocol → half-block → reference
# --------------------------------------------------------------------------


class _FakeProtocol:
    """A stand-in image protocol that records the encode call and emits a marker."""

    name = "fake"

    def __init__(self, seq="\x1b_GFAKE\x1b\\"):
        self._seq = seq
        self.encoded = []

    def encode(self, path, *, max_cols=0, max_rows=0):
        self.encoded.append((path, max_cols, max_rows))
        return self._seq


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_media_block_uses_native_protocol_when_present(tmp_path):
    # A detected protocol wins: the raw escape sequence is written straight to the
    # console file (bypassing rich), and the half-block path is never reached.
    from mote.cli.contracts.view import MediaBlock

    pytest.importorskip("PIL")
    from PIL import Image

    img = tmp_path / "pic.png"
    Image.new("RGB", (8, 8), (9, 9, 9)).save(str(img))

    console = _rich_console()
    driver, surface = _terminal_pair(console)
    proto = _FakeProtocol()
    surface._image_protocol = proto  # inject a detected protocol
    driver.on_unhandled(MediaBlock(media_kind="image", ref=str(img)))

    out = console.file.getvalue()
    assert "\x1b_GFAKE\x1b\\" in out  # the native escape sequence landed
    assert proto.encoded and proto.encoded[0][0] == str(img)
    assert proto.encoded[0][1] > 0  # a cell-width budget was passed


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_media_block_falls_back_to_half_block_without_protocol(tmp_path):
    # No native protocol → the half-block renderer paints truecolor cells.
    from mote.cli.contracts.view import MediaBlock

    pytest.importorskip("PIL")
    from PIL import Image

    img = tmp_path / "pic.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(str(img))

    console = _rich_console()
    driver, surface = _terminal_pair(console)
    surface._image_protocol = None  # force the half-block fallback
    driver.on_unhandled(MediaBlock(media_kind="image", ref=str(img)))

    out = console.file.getvalue()
    assert "\u2580" in out  # ▀ half-block glyph
    assert "\x1b_G" not in out  # never emitted a native sequence


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_media_block_missing_file_prints_reference_only(tmp_path):
    # A non-existent image can't render either way → only the caption line prints.
    from mote.cli.contracts.view import MediaBlock

    console = _rich_console()
    driver, surface = _terminal_pair(console)
    surface._image_protocol = _FakeProtocol()
    driver.on_unhandled(MediaBlock(media_kind="image", ref="/no/such.png"))

    out = console.file.getvalue()
    assert "/no/such.png" in out  # caption reference
    assert "\x1b_GFAKE" not in out  # protocol not invoked for a missing file
    assert "\u2580" not in out  # no half-block either


# --------------------------------------------------------------------------
# on_file_diff_block — a structured change renders as a coloured diff
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_file_diff_block_renders_caption_and_diff():
    from mote.cli.contracts.view import FileDiffBlock

    console = _rich_console()
    driver, _ = _terminal_pair(console)
    driver.on_unhandled(FileDiffBlock(path="/tmp/a.py", old="x = 1\n", new="x = 2\n"))
    out = console.file.getvalue()
    assert "/tmp/a.py" in out  # caption names the file
    assert "(updated)" in out  # both sides present → updated
    # The synthesized unified diff was rendered (its --- header survives; the
    # body digits are split by word-level SGR spans so assert the header instead).
    assert "---" in out and "/tmp/a.py" in out


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_file_diff_block_caption_verb_reflects_create_delete():
    from mote.cli.contracts.view import FileDiffBlock

    console = _rich_console()
    driver, _ = _terminal_pair(console)
    driver.on_unhandled(FileDiffBlock(path="/tmp/new.py", old="", new="hi\n"))
    driver.on_unhandled(FileDiffBlock(path="/tmp/gone.py", old="bye\n", new=""))
    out = console.file.getvalue()
    assert "(created)" in out
    assert "(deleted)" in out


def test_plain_file_diff_block_prints_verb_and_path():
    from mote.cli.contracts.view import FileDiffBlock

    buf = io.StringIO()
    c = PlainTerminalConsumer(out=buf)
    c.on_file_diff_block(FileDiffBlock(path="/tmp/a.py", old="x = 1\n", new="x = 2\n"))
    assert buf.getvalue() == "  [updated] /tmp/a.py\n"


# --------------------------------------------------------------------------
# PortHumanChannel — ask_user → port.ask
# --------------------------------------------------------------------------


class FakePort:
    def __init__(self, answer: str = "yes"):
        self._answer = answer
        self.asked = []

    async def ask(self, ctx, question: str) -> str:
        self.asked.append((ctx, question))
        return self._answer


class FakeApprovalPort(FakePort):
    """A port that also exposes the structured ``decide_approval`` selector."""

    def __init__(self, outcome: str = "accept"):
        super().__init__()
        self._outcome = outcome
        self.decided = []

    async def decide_approval(self, ctx, request):
        from mote.cli.contracts.view.events import ApprovalDecision

        self.decided.append((ctx, request))
        return ApprovalDecision(approval_id="", outcome=self._outcome)


@pytest.mark.asyncio
async def test_human_channel_ask_delegates_to_port():
    port = FakePort(answer="42")
    env = PortHumanChannel(port, ctx="CTX")
    ans = await env.ask_user("How many?")
    assert ans == "42"
    assert port.asked == [("CTX", "How many?")]


@pytest.mark.asyncio
async def test_human_channel_approval_prompt_routes_to_selector():
    # An engine-rendered approval prompt drives decide_approval (not ask) and the
    # chosen outcome maps back to the engine's free-text reply vocabulary.
    prompt = (
        "[APPROVAL REQUIRED] The agent wants to run tool 'Bash'.\n"
        "  target: rm -rf build/\n"
        "Reply 'yes' to allow once, 'always' to allow for the rest of the session, or 'no' to deny."
    )
    for outcome, expected in [
        ("accept", "yes"),
        ("always_allow", "always"),
        ("reject", "no"),
        ("always_deny", "no"),
    ]:
        port = FakeApprovalPort(outcome=outcome)
        env = PortHumanChannel(port, ctx="CTX")
        reply = await env.ask_user(prompt)
        assert reply == expected
        assert port.asked == []  # never fell through to the text input
        assert len(port.decided) == 1
        req = port.decided[0][1]
        assert req.action == "run: Bash"
        assert "rm -rf build/" in req.args_preview
        assert "Reply 'yes'" not in req.args_preview  # instruction line stripped


@pytest.mark.asyncio
async def test_human_channel_escalation_prompt_routes_to_selector():
    prompt = (
        "[SANDBOX ESCALATION] Tool 'Write' wants to write outside the sandbox:\n"
        "  path:   /etc/hosts\n"
        "Reply 'yes' to allow this write once, 'always' to allow, or 'no' to block it."
    )
    port = FakeApprovalPort(outcome="always_allow")
    env = PortHumanChannel(port)
    reply = await env.ask_user(prompt)
    assert reply == "always"
    assert port.decided[0][1].risk == "high"


@pytest.mark.asyncio
async def test_human_channel_plain_question_ignores_selector():
    # A non-approval question still uses the plain text ``ask`` even on a port
    # that has decide_approval.
    port = FakeApprovalPort()
    port._answer = "the answer"
    env = PortHumanChannel(port, ctx="C")
    ans = await env.ask_user("What is your favorite color?")
    assert ans == "the answer"
    assert port.decided == []
    assert port.asked == [("C", "What is your favorite color?")]


class FakeQuestionsPort(FakePort):
    """A port exposing the structured ``ask_questions`` round-trip.

    Records the received ``AskUserQuestionInput`` and returns a scripted
    ``AskUserQuestionAnswers`` — the zero-text-parsing path (§7/§8).
    """

    def __init__(self, answers=None):
        super().__init__()
        self._answers = answers
        self.questions_calls = []

    async def ask_questions(self, ctx, questions):
        from mote.common.schema import AskUserQuestionAnswers

        self.questions_calls.append((ctx, questions))
        return self._answers if self._answers is not None else AskUserQuestionAnswers()


def _q(question, header, options, multiSelect=False):
    return {
        "question": question,
        "header": header,
        "options": [{"label": l, "description": d} for l, d in options],
        "multiSelect": multiSelect,
    }


def _questions(*qs):
    from mote.common.schema import AskUserQuestionInput

    # Mirror AskUserQuestion._coerce: the channel receives the plain list of items.
    return AskUserQuestionInput.model_validate({"questions": list(qs)}).questions


def _answers(*answers):
    from mote.common.schema import AskUserQuestionAnswer, AskUserQuestionAnswers

    return AskUserQuestionAnswers(
        answers=[AskUserQuestionAnswer(question=q, selected=list(sel), free_text=free) for q, sel, free in answers]
    )


@pytest.mark.asyncio
async def test_human_channel_askuserquestion_routes_to_ask_questions():
    # A structured AskUserQuestion round-trip flows through ``ask_questions``
    # unchanged: no text rendering, the typed input goes down, answers come up.
    from mote.common.schema import AskUserQuestionAnswers

    port = FakeQuestionsPort(answers=_answers(("Pick a color", ["Blue"], "")))
    env = PortHumanChannel(port, ctx="C")
    result = await env.ask_user_question(_questions(_q("Pick a color", "Color", [("Red", "warm"), ("Blue", "cool")])))
    assert isinstance(result, AskUserQuestionAnswers)
    assert result.answers[0].selected == ["Blue"]
    assert len(port.questions_calls) == 1
    ctx, questions = port.questions_calls[0]
    assert ctx == "C"
    assert questions[0].question == "Pick a color"


@pytest.mark.asyncio
async def test_human_channel_askuserquestion_multiline_free_text_verbatim():
    # Regression #1/#3: a multi-line + numeric free-text answer survives intact.
    port = FakeQuestionsPort(answers=_answers(("Notes?", [], "para one\n\npara two\n\n42")))
    env = PortHumanChannel(port)
    result = await env.ask_user_question(_questions(_q("Notes?", "N", [("A", ""), ("B", "")])))
    assert result.answers[0].free_text == "para one\n\npara two\n\n42"


@pytest.mark.asyncio
async def test_human_channel_askuserquestion_degrades_without_ask_questions():
    # A port that predates ``ask_questions`` degrades per-question through the
    # plain ``ask``, still building STRUCTURED answers (no block-split / pairing).
    from mote.common.schema import AskUserQuestionAnswers

    class DegradePort(FakePort):
        def __init__(self, replies):
            super().__init__()
            self._replies = list(replies)
            self.asked = []

        async def ask(self, ctx, question, options=None, multi=False):
            self.asked.append((question, options, multi))
            return self._replies.pop(0)

    port = DegradePort(["Blue", "custom text"])
    env = PortHumanChannel(port, ctx="C")
    result = await env.ask_user_question(
        _questions(
            _q("Color?", "C", [("Red", ""), ("Blue", "")]),
            _q("Notes?", "N", [("A", ""), ("B", "")]),
        )
    )
    assert isinstance(result, AskUserQuestionAnswers)
    # Q1 answer matched a label → selected; Q2 answer was free text.
    assert result.answers[0].selected == ["Blue"]
    assert result.answers[0].free_text == ""
    assert result.answers[1].selected == []
    assert result.answers[1].free_text == "custom text"


@pytest.mark.asyncio
async def test_human_channel_degrade_falls_back_to_2arg_ask():
    # A port whose ``ask`` only accepts (ctx, question) still degrades cleanly.
    from mote.common.schema import AskUserQuestionAnswers

    port = FakePort(answer="Red")  # 2-arg ask, no options kwarg
    env = PortHumanChannel(port, ctx="C")
    result = await env.ask_user_question(_questions(_q("Pick", "P", [("Red", ""), ("Blue", "")])))
    assert isinstance(result, AskUserQuestionAnswers)
    assert result.answers[0].selected == ["Red"]


@pytest.mark.asyncio
async def test_human_channel_reply_is_empty():
    env = PortHumanChannel(FakePort())
    assert await env.reply_to_user("anything") == ""


def test_human_channel_is_inert_for_team_surface():
    env = PortHumanChannel(FakePort())
    assert env.desc == ""
    assert env.roles == {}
    assert env.role_names() == []
    # no-ops must not raise
    env.set_addresses(object(), object())
    env.publish_message(object())
