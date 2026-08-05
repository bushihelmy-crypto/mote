#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`SessionDriver` — the thin orchestration loop (§2.6).

Two concerns: the **command host surface** (the ``ctx`` the registry dispatches
on — ``notice`` / ``request_exit`` / agent-lifecycle resolution) and the **turn
mechanics** (``_run_turn`` arbitrating the lock, surfacing a turn error as an
``ErrorRaised`` ViewEvent rather than reading ``context.messages``, and the
mid-turn interrupt staging). Fakes stand in for the control plane / role / port /
projector so the driver's orchestration is testable without real agents or I/O.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from mote.product.entrypoints.cli import backend as cli_backend
from mote.product.i18n import keys as K
from mote.product.i18n import t
from mote.product.interaction.driver import SessionDriver, _format_turn_error
from mote.product.presentation.events import ErrorRaised, MessageBlockCompleted, Notice, TranscriptCleared

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeContextManager:
    """Duck-typed ContextManager message store: count + clear."""

    def __init__(self, n: int = 0) -> None:
        self._n = n
        self.cleared = False

    def count(self) -> int:
        return self._n

    async def clear(self) -> None:
        self.cleared = True
        self._n = 0


class FakeRole:
    def __init__(
        self,
        session_id: str = "sess-0001",
        name: str = "Assistant",
        tools: Optional[List[str]] = None,
        mcps: Optional[List[str]] = None,
        deferred_tools: Optional[List[str]] = None,
    ) -> None:
        self.session_id = session_id
        self.state = SimpleNamespace(env=None)
        self.role_schema = SimpleNamespace(
            name=name, tools=tools or [], mcps=mcps or [], deferred_tools=deferred_tools or []
        )
        self.config = SimpleNamespace(tools=SimpleNamespace(tool_search=SimpleNamespace(enabled=True)))
        self.telemetry = None
        self.context_manager = FakeContextManager()

    def list_deferred_tools(self) -> dict[str, str]:
        return {name: name for name in self.role_schema.deferred_tools}


class FakeRuntime:
    def __init__(self, role: FakeRole, last_error: Optional[BaseException] = None) -> None:
        self.role = role
        self.last_error = last_error


class FakeControl:
    def __init__(self, runtimes: Optional[dict] = None) -> None:
        self._runtimes = runtimes or {}
        self.started = False
        self.stopped = False
        self.inputs: List[Any] = []
        self.interrupts: List[str] = []
        self.added: List[Any] = []
        self._quiescent_seq: Optional[List[bool]] = None

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def runtimes(self) -> dict:
        return dict(self._runtimes)

    def get_runtime(self, aid: str):
        return self._runtimes.get(aid)

    def get_status(self, aid: str):
        return SimpleNamespace(value="idle")

    def send_input(self, aid: str, msg: Any) -> None:
        self.inputs.append((aid, msg))

    def quiescent(self) -> bool:
        if self._quiescent_seq:
            return self._quiescent_seq.pop(0)
        return True

    async def interrupt(self, aid: str) -> None:
        self.interrupts.append(aid)

    def add_agent(self, runtime: Any, root: bool = False) -> None:
        self.added.append((runtime, root))


class FakeProjector:
    def __init__(self) -> None:
        self.delivered: List[Any] = []
        self.delivered_sync: List[Any] = []

    async def deliver(self, ev: Any) -> None:
        self.delivered.append(ev)

    def deliver_sync(self, ev: Any) -> None:
        self.delivered_sync.append(ev)

    async def aclose(self) -> None:
        return None


class FakePort:
    def __init__(self) -> None:
        self._on_interrupt = None
        self._is_turn_running = None
        self.exit_requested = False
        self.staged: List[str] = []

    def bind_driver_control(self, binding) -> None:
        self._on_interrupt = binding.interrupt
        self._is_turn_running = binding.turn_running

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def take_turn_images(self) -> list[dict]:
        return []

    def request_exit(self) -> None:
        self.exit_requested = True

    def stage_restore(self, text: str) -> None:
        self.staged.append(text)


def make_driver(*, agent_id: str = "sess-0001", runtimes: Optional[dict] = None, role_factory=None):
    role = FakeRole(session_id=agent_id)
    control = FakeControl(runtimes if runtimes is not None else {agent_id: FakeRuntime(role)})
    port = FakePort()
    projector = FakeProjector()
    drv = SessionDriver(
        control,
        agent_id,
        role,
        backend=cli_backend,
        port=port,
        projector=projector,
        role_factory=role_factory,
        agent_catalog=object(),
    )
    return drv, control, port, projector


# --------------------------------------------------------------------------
# Constructor wiring
# --------------------------------------------------------------------------


def test_constructor_wires_port_interrupt_hooks():
    drv, _control, port, _proj = make_driver()
    # The driver injects its interrupt + turn-state hooks onto the port.
    assert port._on_interrupt == drv._interrupt_current_turn
    assert port._is_turn_running() is False  # no turn running yet


# --------------------------------------------------------------------------
# Command host surface
# --------------------------------------------------------------------------


def test_notice_delivers_notice_on_every_consumer():
    drv, _c, _p, projector = make_driver()
    drv.notice("hello", level="warning")
    assert len(projector.delivered_sync) == 1
    ev = projector.delivered_sync[0]
    assert isinstance(ev, Notice)
    assert ev.text == "hello" and ev.level == "warning"


def test_announce_tools_flags_builtin_count():
    # MCP servers are present but must NOT be counted: they connect lazily and
    # surface per-turn in the system-reminder catalog, not at the one-time
    # startup load the badge reports.
    role = FakeRole(tools=["Read", "Write", "Bash"], mcps=["fs", "remote"])
    control = FakeControl({role.session_id: FakeRuntime(role)})
    drv = SessionDriver(
        control,
        role.session_id,
        role,
        backend=cli_backend,
        port=FakePort(),
        projector=FakeProjector(),
    )
    drv._announce_tools()
    ev = drv._projector.delivered_sync[-1]
    assert isinstance(ev, Notice)
    assert "\u2691" in ev.text
    assert t(K.DRIVER_TOOLS_LOADED, count=3, loaded=3, deferred=0) in ev.text
    assert "MCP" not in ev.text


def test_announce_tools_annotates_deferred_count():
    # Deferred (search-to-enable) tools are part of the startup load — they count
    # toward the total, and the badge annotates how many of it start deferred.
    role = FakeRole(tools=["Read", "Write", "Bash"], deferred_tools=["WebBrowser", "Agent"])
    control = FakeControl({role.session_id: FakeRuntime(role)})
    drv = SessionDriver(
        control,
        role.session_id,
        role,
        backend=cli_backend,
        port=FakePort(),
        projector=FakeProjector(),
    )
    drv._announce_tools()
    ev = drv._projector.delivered_sync[-1]
    assert isinstance(ev, Notice)
    assert t(K.DRIVER_TOOLS_LOADED, count=3, loaded=1, deferred=2) in ev.text


def test_announce_tools_no_op_when_no_tools():
    role = FakeRole(tools=[], mcps=[])
    control = FakeControl({role.session_id: FakeRuntime(role)})
    projector = FakeProjector()
    drv = SessionDriver(
        control,
        role.session_id,
        role,
        backend=cli_backend,
        port=FakePort(),
        projector=projector,
    )
    drv._announce_tools()
    assert projector.delivered_sync == []


def test_request_exit_sets_flag_and_signals_port():
    drv, _c, port, _p = make_driver()
    drv.request_exit()
    assert drv._exit is True
    assert port.exit_requested is True


def test_help_text_delegates_to_registry():
    drv, _c, _p, _proj = make_driver()
    assert drv.help_text().startswith("Commands:")


def test_current_agent_id_property():
    drv, _c, _p, _proj = make_driver(agent_id="sess-XYZ")
    assert drv.current_agent_id == "sess-XYZ"


def test_active_agents_lists_id_name_status():
    role = FakeRole(session_id="a1", name="Helper")
    drv, _c, _p, _proj = make_driver(agent_id="a1", runtimes={"a1": FakeRuntime(role)})
    agents = drv.active_agents()
    assert agents == [("a1", "Helper", "idle")]


@pytest.mark.asyncio
async def test_clear_conversation_clears_history_and_emits_transcript_cleared():
    drv, _c, _p, projector = make_driver()
    drv._role.context_manager = FakeContextManager(n=5)
    cleared = await drv.clear_conversation()
    assert cleared == 5  # returns the pre-clear message count
    assert drv._role.context_manager.cleared is True
    assert drv._role.context_manager.count() == 0
    # every consumer is told to wipe its rendered transcript
    assert len(projector.delivered_sync) == 1
    assert isinstance(projector.delivered_sync[0], TranscriptCleared)


# --------------------------------------------------------------------------
# Scheduler lifecycle (optional background scheduler, e.g. CronService)
# --------------------------------------------------------------------------


class FakeScheduler:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class RunOncePort:
    """A port whose read_turn returns None so run() breaks after one pass."""

    def __init__(self) -> None:
        self._on_interrupt = None
        self._is_turn_running = None
        self._on_steer = None
        self.closed = False

    def bind_driver_control(self, binding) -> None:
        self._on_interrupt = binding.interrupt
        self._is_turn_running = binding.turn_running
        self._on_steer = binding.steer

    def stage_restore(self, text: str) -> None:
        return None

    async def start(self) -> None:
        pass

    async def read_turn(self):
        return None

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_scheduler_started_and_stopped_across_run(monkeypatch):
    monkeypatch.setattr(cli_backend, "role_telemetry", lambda role: None)
    monkeypatch.setattr(cli_backend, "bind_human_channel", lambda role, ch: None)
    monkeypatch.setattr(cli_backend, "role_tool_count", lambda role: 0)
    monkeypatch.setattr(cli_backend, "role_cleanup", lambda role: None)

    role = FakeRole()
    control = FakeControl({role.session_id: FakeRuntime(role)})
    sched = FakeScheduler()
    drv = SessionDriver(
        control,
        role.session_id,
        role,
        backend=cli_backend,
        port=RunOncePort(),
        projector=FakeProjector(),
        scheduler=sched,
    )
    await drv.run()
    assert sched.started is True
    assert sched.stopped is True
    assert control.started is True
    assert control.stopped is True


@pytest.mark.asyncio
async def test_teardown_without_scheduler_is_fine():
    drv, _c, port, _p = make_driver()
    # Default make_driver passes no scheduler; teardown must not choke.
    await drv._teardown()
    assert drv._scheduler is None


# --------------------------------------------------------------------------
# Reference resolution
# --------------------------------------------------------------------------


def test_resolve_ref_by_index_exact_and_prefix():
    ids = ["alpha", "beta", "gamma"]
    assert SessionDriver._resolve_ref("1", ids) == "beta"  # index
    assert SessionDriver._resolve_ref("gamma", ids) == "gamma"  # exact
    assert SessionDriver._resolve_ref("al", ids) == "alpha"  # unique prefix


def test_resolve_ref_out_of_range_and_ambiguous_return_none():
    ids = ["abc", "abd"]
    assert SessionDriver._resolve_ref("9", ids) is None  # index OOB
    assert SessionDriver._resolve_ref("ab", ids) is None  # ambiguous prefix
    assert SessionDriver._resolve_ref("zzz", ids) is None  # no match


def test_switch_agent_success_returns_id_and_name():
    r1 = FakeRole(session_id="a1", name="One")
    r2 = FakeRole(session_id="a2", name="Two")
    drv, _c, _p, _proj = make_driver(agent_id="a1", runtimes={"a1": FakeRuntime(r1), "a2": FakeRuntime(r2)})
    result = drv.switch_agent("Two")
    assert result == ("a2", "Two")
    assert drv.current_agent_id == "a2"


def test_switch_agent_not_found_returns_none():
    drv, _c, _p, _proj = make_driver()
    assert drv.switch_agent("nope") is None


# --------------------------------------------------------------------------
# Agent lifecycle without a role factory
# --------------------------------------------------------------------------


def test_new_agent_without_factory_returns_none():
    drv, _c, _p, _proj = make_driver(role_factory=None)
    assert drv.new_agent("Bob") is None


@pytest.mark.asyncio
async def test_fork_current_without_fork_session_returns_none():
    drv, _c, _p, _proj = make_driver()  # FakeRole has no fork_session
    assert await drv.fork_current() is None


def test_resume_already_loaded_session_switches():
    drv, _c, _p, _proj = make_driver(agent_id="sess-12345678")
    ok, msg = drv.resume_session_ref("sess-12345678")
    assert ok is True
    assert "already-loaded" in msg


# --------------------------------------------------------------------------
# Typed agent spawn (agent_registry passthrough)
# --------------------------------------------------------------------------


def test_spawn_agent_type_known_adopts_and_returns_id_and_name():
    made = {}

    def factory(request):
        made["agent_type"] = request.agent_type
        made["name"] = request.name
        return FakeRole(session_id="typed-0001", name=request.name)

    drv, control, _p, _proj = make_driver(role_factory=factory)
    sid, name = drv.spawn_agent_type("Coder", "Bob")
    assert made["agent_type"] == "Coder"
    assert (sid, name) == ("typed-0001", "Bob")
    # the typed role was adopted into the control plane
    assert control.added  # add_agent was called


def test_spawn_agent_type_defaults_name_to_type():
    def factory(request):
        return FakeRole(session_id="typed-0002", name=request.name)

    drv, _c, _p, _proj = make_driver(role_factory=factory)
    sid, name = drv.spawn_agent_type("Coder", "")
    assert name == "Coder"


def test_spawn_agent_type_unknown_returns_none_message():
    def factory(request):
        del request
        return None  # unknown/unavailable type

    drv, _c, _p, _proj = make_driver(role_factory=factory)
    sid, msg = drv.spawn_agent_type("Nope")
    assert sid is None
    assert "Nope" in msg


def test_list_agent_types_delegates_to_backend(monkeypatch):
    monkeypatch.setattr(cli_backend, "list_agent_types", lambda _catalog: [("Coder", "writes code")])
    drv, _c, _p, _proj = make_driver()
    assert drv.list_agent_types() == [("Coder", "writes code")]


# --------------------------------------------------------------------------
# Turn mechanics
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_sends_input_and_awaits_quiescence():
    drv, control, _p, projector = make_driver()
    control._quiescent_seq = [False, False, True]  # busy twice then quiescent
    await drv._run_turn("do the thing")
    assert len(control.inputs) == 1
    aid, msg = control.inputs[0]
    assert aid == "sess-0001"
    assert msg.content == "do the thing"
    # the turn-running flag is cleared and no error surfaced
    assert drv._running_turn is False
    # the only delivered ViewEvent is the user's own message block (transcript)
    assert len(projector.delivered) == 1
    user_ev = projector.delivered[0]
    assert isinstance(user_ev, MessageBlockCompleted)
    assert user_ev.role == "user"
    assert user_ev.markdown == "do the thing"


@pytest.mark.asyncio
async def test_run_turn_attaches_images_as_metadata_and_media_blocks():
    """Prompt-dragged images ride along as ``metadata[IMAGES]`` + a MediaBlock each."""
    from mote.contracts.conversation.fields import IMAGES
    from mote.product.presentation.events import MediaBlock

    drv, control, _p, projector = make_driver()
    images = [
        {"b64": "AAA", "path": "/tmp/a.png", "mime": "image/png"},
        {"b64": "BBB", "path": "/tmp/b.jpg", "mime": "image/jpeg"},
    ]
    await drv._run_turn("look at these", images=images)
    # The UserMessage carries the raw base64 payloads for the LLM's multimodal path.
    _aid, msg = control.inputs[0]
    assert msg.metadata[IMAGES] == ["AAA", "BBB"]
    # A MediaBlock is surfaced for each image so they render in the transcript.
    media = [e for e in projector.delivered if isinstance(e, MediaBlock)]
    assert [m.ref for m in media] == ["/tmp/a.png", "/tmp/b.jpg"]
    assert media[0].mime == "image/png"


@pytest.mark.asyncio
async def test_run_turn_without_images_sets_no_image_metadata():
    """A plain text turn attaches no image metadata and emits no MediaBlock."""
    from mote.contracts.conversation.fields import IMAGES
    from mote.product.presentation.events import MediaBlock

    drv, control, _p, projector = make_driver()
    await drv._run_turn("just text")
    _aid, msg = control.inputs[0]
    assert IMAGES not in msg.metadata
    assert not any(isinstance(e, MediaBlock) for e in projector.delivered)


@pytest.mark.asyncio
async def test_run_turn_surfaces_error_as_view_event():
    role = FakeRole()
    err = ValueError("boom")
    drv, _control, _p, projector = make_driver(runtimes={"sess-0001": FakeRuntime(role, last_error=err)})
    await drv._run_turn("trigger")
    # user message block first, then the surfaced error
    assert len(projector.delivered) == 2
    user_ev, ev = projector.delivered
    assert isinstance(user_ev, MessageBlockCompleted) and user_ev.role == "user"
    assert isinstance(ev, ErrorRaised)
    assert "boom" in ev.text


@pytest.mark.asyncio
async def test_interrupt_current_turn_stages_and_interrupts():
    import asyncio

    drv, control, port, _proj = make_driver()
    drv._current_input = "in-flight prompt"
    receipt = drv._interrupt_current_turn()
    duplicate = drv._interrupt_current_turn()
    assert receipt.disposition.value == "accepted"
    assert duplicate.disposition.value == "already_pending"
    assert port.staged == ["in-flight prompt"]
    await asyncio.sleep(0)  # let the scheduled control.interrupt run
    assert control.interrupts == ["sess-0001"]


def test_steer_submission_returns_typed_receipt():
    drv, _control, _port, _proj = make_driver()

    assert drv._enqueue_steer("later").disposition.value == "accepted"
    assert drv._enqueue_steer("  ").disposition.value == "ignored"


# --------------------------------------------------------------------------
# Error formatting helper
# --------------------------------------------------------------------------


def test_format_turn_error_plain():
    assert _format_turn_error(ValueError("nope")) == "ValueError: nope"


def test_format_turn_error_with_status_code():
    err = RuntimeError("rate limited")
    err.status_code = 429
    assert _format_turn_error(err) == "RuntimeError (HTTP 429): rate limited"
