#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline tests for the interactive REPL.

No real tty, no real LLM, no real control plane — fakes drive the loop:
  * FakeReader  — feeds preset lines then EOF.
  * FakeControl — records send_input/interrupt/start/stop; ``quiescent()`` flips
    False once per turn to simulate exactly one turn of work.
  * FakeRole    — a real ``state.context.messages`` list; each turn appends an
    assistant reply.
"""

from __future__ import annotations

import asyncio
import io
import types

import pytest

from metagpt.cli.repl import Repl


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeReader:
    """Yield queued lines (as bytes), then EOF (empty bytes) forever."""

    def __init__(self, lines):
        self._queue = list(lines)

    async def readline(self):
        if self._queue:
            # Real readline keeps the trailing newline; a blank line is "\n",
            # never empty (empty == EOF).
            return (self._queue.pop(0) + "\n").encode()
        return b""  # EOF


class FakeReply:
    def __init__(self, content, role="assistant"):
        self.content = content
        self.role = role


class FakeContext:
    def __init__(self):
        self.messages = []


class FakeState:
    def __init__(self):
        self.context = FakeContext()
        self.working_dir = "/tmp"


class FakeRole:
    def __init__(self):
        self.state = FakeState()
        self.session_id = "sess-1"
        self._executor = None


class FakeControl:
    """Records control-plane interactions; one turn => one non-quiescent poll.

    When ``error`` is set, a turn appends *no* assistant reply and the runtime
    reports that error via ``get_runtime(...).last_error`` — simulating an
    ERRORED turn so the REPL surfaces it instead of a blank reply.
    """

    def __init__(self, role, *, reply="hi there", error=None):
        self._role = role
        self._reply = reply
        self._error = error
        self._busy = False
        self.started = False
        self.stopped = False
        self.inputs = []
        self.interrupts = []

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def send_input(self, agent_id, message):
        self.inputs.append((agent_id, message))
        if self._error is None:
            # Simulate the turn completing: append an assistant reply now.
            self._role.state.context.messages.append(FakeReply(self._reply))
        self._busy = True

    def quiescent(self):
        if self._busy:
            self._busy = False
            return False
        return True

    def get_runtime(self, agent_id):
        return types.SimpleNamespace(last_error=self._error)

    async def interrupt(self, agent_id):
        self.interrupts.append(agent_id)
        self._busy = False
        return None


def make_repl(lines, *, reply="hi there", out=None, error=None):
    role = FakeRole()
    control = FakeControl(role, reply=reply, error=error)
    out = out if out is not None else io.StringIO()
    repl = Repl(
        control,
        role.session_id,
        role,
        out=out,
        get_input_reader=lambda: FakeReader(lines),
        double_press_window=2.0,
    )
    return repl, control, role, out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_multiple_lines_trigger_one_send_each():
    repl, control, role, out = make_repl(["hello", "world"])
    asyncio.run(repl.run())

    assert len(control.inputs) == 2
    assert control.inputs[0][1].content == "hello"
    assert control.inputs[1][1].content == "world"
    # Each turn appended one assistant reply, both printed.
    text = out.getvalue()
    assert text.count("hi there") == 2
    assert control.started is True
    assert control.stopped is True


def test_errored_turn_surfaces_error_plain():
    # A turn that ends in ERRORED leaves no assistant reply; the REPL must show
    # the failure (here a typed LLM error with a status code) not a blank prompt.
    from metagpt.common.exception.llm import LLMServerError

    err = LLMServerError("invalid csrf token", status_code=500)
    repl, control, role, out = make_repl(["你好"], error=err)
    asyncio.run(repl.run())

    text = out.getvalue()
    assert "[error]" in text
    assert "LLMServerError" in text
    assert "HTTP 500" in text
    assert "invalid csrf token" in text


def test_errored_turn_routes_to_renderer():
    from metagpt.common.exception.llm import LLMServerError

    err = LLMServerError("boom", status_code=500)
    repl, control, role, out = make_repl(["go"], error=err)
    fake = FakeRenderer()
    repl._renderer = fake
    asyncio.run(repl.run())

    assert len(fake.errors) == 1
    assert "LLMServerError" in fake.errors[0]
    # No assistant reply on an errored turn.
    assert fake.assistants == []


def test_successful_turn_shows_no_error():
    repl, control, role, out = make_repl(["hi"])  # error=None
    fake = FakeRenderer()
    repl._renderer = fake
    asyncio.run(repl.run())

    assert fake.errors == []
    assert fake.assistants == ["hi there"]


def test_eof_exits():
    repl, control, role, out = make_repl([])  # immediate EOF
    asyncio.run(repl.run())
    assert control.inputs == []
    assert repl._should_exit is True
    assert control.stopped is True


def test_blank_lines_are_skipped():
    repl, control, role, out = make_repl(["", "   ", "real"])
    asyncio.run(repl.run())
    assert len(control.inputs) == 1
    assert control.inputs[0][1].content == "real"


def test_sigint_during_turn_interrupts_without_exit():
    repl, control, role, out = make_repl([])

    async def scenario():
        repl._running_turn = True
        repl._on_sigint()
        await asyncio.sleep(0)  # let ensure_future(interrupt) run
        return

    asyncio.run(scenario())
    assert control.interrupts == [role.session_id]
    assert repl._should_exit is False
    assert "interrupting current turn" in out.getvalue()


def test_sigint_idle_double_press_exits():
    repl, control, role, out = make_repl([])

    class FakeTask:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    task = FakeTask()
    repl._read_task = task
    repl._running_turn = False

    # First press: arm window, do not exit.
    repl._on_sigint()
    assert repl._should_exit is False
    assert task.cancelled is False
    assert "Press Ctrl-C again to exit" in out.getvalue()

    # Second press within window: exit + cancel the pending read.
    repl._on_sigint()
    assert repl._should_exit is True
    assert task.cancelled is True


def test_console_ask_prints_question_and_reads_answer():
    repl, control, role, out = make_repl(["option 2"])

    async def scenario():
        await repl._setup_stdin()  # installs the injected FakeReader
        return await repl._console_ask("Pick one:")

    answer = asyncio.run(scenario())
    assert answer == "option 2"
    assert "Pick one:" in out.getvalue()


def test_console_human_channel_delegates_ask():
    from metagpt.cli.repl import _ConsoleHumanChannel

    calls = []

    async def fake_ask(q):
        calls.append(q)
        return "answered"

    channel = _ConsoleHumanChannel(fake_ask)
    result = asyncio.run(channel.ask_human("How?", sent_from="X"))
    assert result == "answered"
    assert calls == ["How?"]
    # Non-ask env methods are inert no-ops.
    assert asyncio.run(channel.reply_to_human("hi")) == ""
    channel.set_addresses(object(), set())
    channel.publish_message(object())


def test_sigint_during_turn_stages_input_for_restore():
    repl, control, role, out = make_repl([])

    async def scenario():
        repl._running_turn = True
        repl._current_input = "build me a thing"
        repl._on_sigint()
        await asyncio.sleep(0)  # let ensure_future(interrupt) run

    asyncio.run(scenario())
    assert repl._restored_input == "build me a thing"
    assert control.interrupts == [role.session_id]


def test_read_line_restores_on_bare_enter():
    repl, control, role, out = make_repl([""])  # user just presses Enter

    async def scenario():
        await repl._setup_stdin()
        repl._restored_input = "previous prompt"
        return await repl._read_line()

    line = asyncio.run(scenario())
    assert line == "previous prompt"
    assert "interrupted" in out.getvalue()
    assert "previous prompt" in out.getvalue()
    assert repl._restored_input is None  # consumed


def test_read_line_restore_replaced_by_new_text():
    repl, control, role, out = make_repl(["a fresh request"])

    async def scenario():
        await repl._setup_stdin()
        repl._restored_input = "previous prompt"
        return await repl._read_line()

    line = asyncio.run(scenario())
    assert line == "a fresh request"
    assert repl._restored_input is None


class FakeRenderer:
    """Records routed calls so we can assert the REPL prefers the renderer."""

    def __init__(self):
        self.writes = []
        self.notices = []
        self.prompts = []
        self.assistants = []
        self.errors = []
        self.end_streams = 0

    def end_stream(self):
        self.end_streams += 1

    def write(self, text):
        self.writes.append(text)

    def notice(self, text):
        self.notices.append(text)

    def prompt(self, p):
        self.prompts.append(p)

    def assistant(self, text):
        self.assistants.append(text)

    def error(self, text):
        self.errors.append(text)


def test_renderer_routes_write_notice_prompt_assistant():
    repl, control, role, out = make_repl([])
    fake = FakeRenderer()
    repl._renderer = fake

    repl._write("plain")
    repl._notice("heads up")
    repl._reprompt()
    role.state.context.messages.append(FakeReply("the answer"))
    repl._print_new_assistant_messages(0)

    assert fake.writes == ["plain"]
    assert fake.notices == ["heads up"]
    assert fake.prompts == [repl._prompt]
    assert fake.assistants == ["the answer"]
    # Nothing leaked to the plain-text stream.
    assert out.getvalue() == ""


def test_no_renderer_keeps_plain_text_path():
    repl, control, role, out = make_repl([])
    repl._write("plain")
    repl._notice("heads up")
    assert "plain" in out.getvalue()
    assert "heads up" in out.getvalue()


def test_stream_sink_mirrors_and_flags_turn():
    repl, control, role, out = make_repl([])  # no renderer -> plain stdout
    repl._stream_sink("tok-1")
    repl._stream_sink("tok-2")
    assert repl._streamed_this_turn is True
    assert out.getvalue() == "tok-1tok-2"


def test_stream_subscriber_forwards_bus_deltas():
    # The REPL mirrors streamed tokens off the role's event bus (no global sink):
    # subscribing on the bus then emitting a delta drives ``_stream_sink``.
    from metagpt.common.events import EventBus, LLMStreamDeltaEvent, set_bus
    from metagpt.common.logs import log_llm_stream

    repl, control, role, out = make_repl([])  # no renderer -> plain stdout
    bus = EventBus()
    role.event_bus = bus
    repl._subscribe_stream(role)
    # log_llm_stream emits LLMStreamDeltaEvent synchronously onto the active bus.
    with set_bus(bus):
        log_llm_stream("hello ")
        log_llm_stream("world")
    assert repl._streamed_this_turn is True
    assert out.getvalue() == "hello world"
    # Teardown unsubscribes; further emits no longer reach the sink.
    repl._unsubscribe_streams()
    with set_bus(bus):
        log_llm_stream("!")
    assert out.getvalue() == "hello world"


def test_streamed_text_not_reprinted_in_plain_mode():
    # When tokens already streamed to plain stdout, the post-turn print must skip
    # the reply to avoid duplicating it verbatim.
    repl, control, role, out = make_repl([])
    repl._streamed_this_turn = True
    role.state.context.messages.append(FakeReply("the answer"))
    repl._print_new_assistant_messages(0)
    assert "the answer" not in out.getvalue()


def test_streamed_text_not_rerendered_with_renderer():
    # With a renderer the live Markdown region already rendered the final reply
    # while streaming, so the post-turn print must NOT render it again.
    repl, control, role, out = make_repl([])
    fake = FakeRenderer()
    repl._renderer = fake
    repl._streamed_this_turn = True
    role.state.context.messages.append(FakeReply("the answer"))
    repl._print_new_assistant_messages(0)
    assert fake.assistants == []


def test_non_streamed_reply_rendered_with_renderer():
    # A non-streaming provider lands the reply only in context, so it is rendered
    # by the post-turn print (the live region never opened).
    repl, control, role, out = make_repl([])
    fake = FakeRenderer()
    repl._renderer = fake
    role.state.context.messages.append(FakeReply("the answer"))
    repl._print_new_assistant_messages(0)
    assert fake.assistants == ["the answer"]


def test_finish_stream_finalizes_renderer_region():
    repl, control, role, out = make_repl([])
    fake = FakeRenderer()
    repl._renderer = fake
    repl._finish_stream()
    assert fake.end_streams == 1


def test_finish_stream_noop_without_renderer():
    # Plain-text mode has no live region; finishing must be a harmless no-op.
    repl, control, role, out = make_repl([])
    repl._finish_stream()  # must not raise


class _Status:
    def __init__(self, value):
        self.value = value


class FakeSchema:
    def __init__(self, name):
        self.name = name


class MultiState:
    def __init__(self):
        self.context = FakeContext()
        self.working_dir = "/tmp"
        self.env = None


class MultiRole:
    """A role rich enough for the multi-agent REPL methods (no real machinery)."""

    _counter = 0

    def __init__(self, name="Assistant", session_id=None, resume_ok=True):
        if session_id is None:
            MultiRole._counter += 1
            session_id = f"sess-{MultiRole._counter:04d}-{name.lower()}"
        self.session_id = session_id
        self.role_schema = FakeSchema(name)
        self.state = MultiState()
        self.resume_ok = resume_ok
        self.resumed = False

    def resume_session(self):
        self.resumed = True
        return self.resume_ok

    def fork_session(self):
        return MultiRole(name=self.role_schema.name + "-fork")


class FakeRuntime:
    def __init__(self, role):
        self.role = role
        self.session_id = role.session_id


class MultiControl:
    """Minimal control plane exposing the surface the REPL's agent methods use."""

    def __init__(self):
        self._runtimes = {}
        self.added = []

    def add_agent(self, runtime, *, root=False):
        self._runtimes[runtime.session_id] = runtime
        self.added.append((runtime.session_id, root))
        return runtime

    def runtimes(self):
        return dict(self._runtimes)

    def get_runtime(self, agent_id):
        return self._runtimes.get(agent_id)

    def get_status(self, agent_id):
        return _Status("idle" if agent_id in self._runtimes else "not_found")


def make_multi_repl(factory_resume_ok=True):
    """Build a Repl wired with a MultiControl + role factory (no renderer)."""
    first = MultiRole(name="Assistant")
    control = MultiControl()
    control.add_agent(FakeRuntime(first), root=True)

    def factory(*, name="Assistant", session_id=None):
        return MultiRole(name=name, session_id=session_id, resume_ok=factory_resume_ok)

    repl = Repl(control, first.session_id, first, out=io.StringIO(), role_factory=factory)
    return repl, control, first


def test_active_agents_and_switch(monkeypatch):
    # adopt_role wraps the role in AgentRuntime (imported from the runtime
    # module at call time); patch it to the lightweight fake.
    monkeypatch.setattr("metagpt.environment.runtime.AgentRuntime", FakeRuntime)
    repl, control, first = make_multi_repl()
    second = MultiRole(name="Worker")
    repl.adopt_role(second, switch=False)

    agents = repl.active_agents()
    names = {n for _, n, _ in agents}
    assert names == {"Assistant", "Worker"}

    # Switch by index, by name, by session-id prefix.
    res = repl.switch_agent("Worker")
    assert res is not None and res[1] == "Worker"
    assert repl.current_agent_id == second.session_id
    assert repl._role is second

    assert repl.switch_agent("Assistant")[1] == "Assistant"
    assert repl.switch_agent("nope") is None


def test_request_exit_cancels_read():
    repl, control, first = make_multi_repl()

    class FakeTask:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    task = FakeTask()
    repl._read_task = task
    repl.request_exit()
    assert repl._should_exit is True
    assert task.cancelled is True


def test_new_agent_adopts_and_switches(monkeypatch):
    monkeypatch.setattr("metagpt.environment.runtime.AgentRuntime", FakeRuntime)
    repl, control, first = make_multi_repl()
    agent_id = repl.new_agent("Researcher")
    assert agent_id is not None
    assert repl.current_agent_id == agent_id
    assert repl._role.role_schema.name == "Researcher"
    # Env channel wired on the adopted role.
    assert repl._role.state.env is not None


def test_fork_current_adopts_child(monkeypatch):
    monkeypatch.setattr("metagpt.environment.runtime.AgentRuntime", FakeRuntime)
    repl, control, first = make_multi_repl()
    agent_id = repl.fork_current()
    assert agent_id is not None
    assert repl.current_agent_id == agent_id
    assert "fork" in repl._role.role_schema.name


def test_new_agent_unavailable_without_factory():
    repl, control, role, out = make_repl([])  # no role_factory injected
    assert repl.new_agent("x") is None


def test_resume_session_ref_success(monkeypatch):
    monkeypatch.setattr("metagpt.environment.runtime.AgentRuntime", FakeRuntime)
    repl, control, first = make_multi_repl(factory_resume_ok=True)
    repl._last_sessions = [types.SimpleNamespace(session_id="abcd1234-target")]
    ok, msg = repl.resume_session_ref("0")
    assert ok is True
    assert "resumed" in msg
    assert "abcd1234-target" in control.runtimes()
    assert repl.current_agent_id == "abcd1234-target"
    assert repl._role.resumed is True


def test_resume_session_ref_no_rollout(monkeypatch):
    monkeypatch.setattr("metagpt.environment.runtime.AgentRuntime", FakeRuntime)
    repl, control, first = make_multi_repl(factory_resume_ok=False)
    repl._last_sessions = [types.SimpleNamespace(session_id="abcd1234-target")]
    ok, msg = repl.resume_session_ref("0")
    assert ok is False
    assert "no rollout" in msg


def test_resume_session_ref_already_loaded(monkeypatch):
    monkeypatch.setattr("metagpt.environment.runtime.AgentRuntime", FakeRuntime)
    repl, control, first = make_multi_repl()
    # Resolve to an already-loaded agent => just switches, no new role.
    repl._last_sessions = [types.SimpleNamespace(session_id=first.session_id)]
    ok, msg = repl.resume_session_ref("0")
    assert ok is True
    assert "already-loaded" in msg
    assert repl.current_agent_id == first.session_id


def test_teardown_stops_control_and_cleans_executor():
    repl, control, role, out = make_repl([])

    cleaned = {"count": 0}

    class FakeExecutor:
        async def cleanup(self):
            cleaned["count"] += 1

    role._executor = FakeExecutor()
    role.executor = role._executor  # property stand-in: attribute access works

    asyncio.run(repl._teardown())
    assert control.stopped is True
    assert cleaned["count"] == 1
