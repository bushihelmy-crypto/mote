#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleState — serializable runtime snapshot for checkpoint/recovery.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import ConfigDict, Field, PrivateAttr

from mote.common.const import DEFAULT_WORKSPACE_ROOT
from mote.common.schema import LLMCallContext, Message, MessageQueue, SerializationMixin, ThinkResult
from mote.session.ids import new_session_id


class RoleState(SerializationMixin):
    """Serializable runtime snapshot, used for cross-machine restoration."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The message sequence fed to the LLM on the last think round — the `req`
    # ContextProvider assembles for aask (history + current user prompt). Kept
    # here so it survives checkpoint/recovery and is the data model a future
    # ContextManager operates on to manage historical context.
    context: LLMCallContext = Field(default_factory=LLMCallContext)

    msg_buffer: MessageQueue = Field(default_factory=MessageQueue, exclude=True)

    # Session
    session_id: str = Field(default_factory=new_session_id)
    # Fork lineage: the session_id this session was forked from (None for roots).
    # Recorded on the rollout's session_meta first line so listing can show the tree.
    parent_session_id: Optional[str] = None
    # Three working-directory paths (Codex-aligned: cwd is stable data, not shell
    # state that drifts with `cd`):
    #   working_dir          — STABLE relative-path resolution base (Bash default
    #                          dir + the stateless file tools). Defaults to the
    #                          startup dir; does NOT follow `cd`. set_cwd remains a
    #                          framework API for an explicit future directory switch,
    #                          but tools never call it automatically.
    #   original_working_dir — set at startup, session-listing/fallback; never moves
    #   project_root         — project identity anchor (skills/context-protocol/memory); never moves
    working_dir: str = Field(default_factory=lambda: str(DEFAULT_WORKSPACE_ROOT.resolve()))
    original_working_dir: str = Field(default_factory=lambda: str(DEFAULT_WORKSPACE_ROOT.resolve()))
    project_root: str = Field(default_factory=lambda: str(DEFAULT_WORKSPACE_ROOT.resolve()))

    # Execution tracking
    latest_observed_msg: Optional[Message] = None
    recovered: bool = False
    last_end_output: str = Field(default="", exclude=True)
    # The most recent think round's output (content + tool_calls). Published here
    # by the loop the moment the think task drains, so a tool running later in the
    # same turn (e.g. ``end_session`` reading the assistant's final text) reads it
    # off state instead of reaching into the think-engine machinery — which lets
    # the engine be a stateless per-turn factory. Runtime-only: this is transient
    # turn output, reconstructed from the replayed message history on resume, so
    # it never rides the durable checkpoint.
    last_think_result: ThinkResult = Field(default_factory=ThinkResult, exclude=True)

    # Routing
    addresses: set[str] = set()
    watch: set[str] = Field(default_factory=set)

    # Tool-search: the set of deferred tools the model has discovered (revealed)
    # this session. Serialized so a resumed session keeps its discovered tools
    # visible (rides the checkpoint like ``addresses``). The executor's tool
    # catalog reads it via a live getter to decide which deferred schemas to
    # withhold; the ``RoleStateController`` owns the validated union.
    revealed_tools: set[str] = Field(default_factory=set)

    # Environment (not serialized)
    env: Optional[Any] = Field(default=None, exclude=True)

    # Internal flags
    _active: bool = PrivateAttr(default=False)

    # Shared file-read state. Maps an
    # absolute path -> the file's mtime_ns at the moment it was last read. The
    # Read tool records here; the Write/Edit tools consult it to enforce
    # read-before-overwrite and to detect files changed since the last read.
    # Runtime-only (not part of the serialized checkpoint).
    _file_read_state: dict[str, int] = PrivateAttr(default_factory=dict)

    # Files the session merely *glimpsed* — surfaced by a Grep/Glob match but not
    # read in full. Distinct from ``_file_read_state`` (which means "body was in
    # context, mtime tracked for read-before-write"); a glimpse carries no body,
    # so it feeds only the code map's navigation view (defines + intent to help
    # the model choose what to Read), never the read-before-write guard or the
    # code map's F1 in-context suppression. An ordered set (dict for insertion
    # order); paths are absolute. Runtime-only, like ``_file_read_state``.
    _file_glimpsed_state: dict[str, None] = PrivateAttr(default_factory=dict)

    # Live, per-Role session state for stateful tools (a persistent terminal
    # shell, a Python kernel, ...), keyed by tool name -> the tool's live
    # session object. The Role owns this store instead of each stateful tool
    # reaching a process-global singleton, so sessions are isolated per Role
    # and torn down with it. Runtime-only: live OS handles (PTY/kernel
    # subprocesses, fds, channels) cannot cross a checkpoint, so they are
    # excluded from serialization and rebuilt lazily on next use; the
    # serializable identity they key off (session_id, working_dir) lives in the
    # fields above and rides the checkpoint as usual.
    _tool_sessions: dict[str, Any] = PrivateAttr(default_factory=dict)

    # Pending persistent-terminal state to restore on the next terminal start,
    # set by ``resume_session`` from the rollout's latest TerminalStateEvent
    # ({cwd, env, unset}). The Terminal tool consumes it once when it spins up a
    # fresh shell (re-seeding cwd/env without re-running user commands), then it
    # is cleared. Runtime-only: like ``_tool_sessions`` it is recomputed from the
    # rollout on resume, never serialized into the checkpoint. Kept separate from
    # ``_tool_sessions`` (whose values are *live session objects*) to avoid
    # polluting that map's semantics with plain restore data.
    _pending_terminal_restore: Optional[dict] = PrivateAttr(default=None)
    # Pending kernel-state to restore on resume ({cwd, env, unset}), the Python
    # sibling of ``_pending_terminal_restore``. The Python tool consumes it once
    # when it spins up a fresh kernel (re-seeding cwd/env without re-running user
    # code), then it is cleared. Runtime-only: recomputed from the rollout on
    # resume, never serialized. Kept separate from ``_pending_terminal_restore``
    # because the kernel and shell restores are independent (different processes,
    # different restore mechanisms) and must not clobber each other.
    _pending_kernel_restore: Optional[dict] = PrivateAttr(default=None)
    # Pending browser-state to restore on resume ({urls, active, storage_state}),
    # the browser sibling of ``_pending_terminal_restore``. The WebBrowser tool
    # consumes it once when it launches a fresh browser (re-opening the saved tabs
    # seeded with the stored session without re-running navigation/click actions),
    # then it is cleared. Runtime-only: recomputed from the rollout on resume,
    # never serialized. Kept separate from the terminal/kernel restores because
    # the browser restore is independent (a different runtime, a different restore
    # mechanism) and must not clobber the others.
    _pending_browser_restore: Optional[dict] = PrivateAttr(default=None)


class RoleStateController:
    """Behaviour over a :class:`RoleState` — keeps the DTO pure.

    ``RoleState`` is a plain serializable snapshot (a transport DTO): it carries
    fields only, no logic. This controller owns the small invariants that guard
    those fields (cwd fallback, falsy-message guard, the active-signal toggle,
    the file-read map). The Role exposes thin delegators onto these methods as
    its capability surface for tools and the framework, so tools never touch the
    raw state and the state stays free of behaviour.

    Holds the state by reference; the reference is stable for a Role's lifetime
    (``RoleState`` is mutated in place, never reassigned).
    """

    def __init__(self, state: "RoleState"):
        self._state = state

    @property
    def state(self) -> "RoleState":
        return self._state

    def get_cwd(self) -> str:
        """Stable relative-path base dir, falling back to the startup dir; never empty."""
        try:
            return self._state.working_dir or self._state.original_working_dir
        except Exception:
            return self._state.original_working_dir

    def set_cwd(self, path: str) -> None:
        """Set the stable working directory (framework API for an explicit switch).

        Not called automatically by the Bash tool — a `cd` inside a command
        does not drift the cwd (Codex-aligned). Provided as a deliberate
        directory-change entry point.
        """
        self._state.working_dir = path

    def record_file_read(self, path: str, mtime_ns: int) -> None:
        """Record the mtime_ns observed when `path` was last read."""
        self._state._file_read_state[path] = mtime_ns

    def get_file_read_mtime(self, path: str) -> Optional[int]:
        """Return the mtime_ns recorded when `path` was last read, else None."""
        return self._state._file_read_state.get(path)

    def record_file_glimpsed(self, path: str) -> None:
        """Record that `path` surfaced in a search result (Grep/Glob), un-read.

        A glimpse (no body) feeds only the code map's navigation view, so it is
        stored separately from the read state and never affects the
        read-before-write guard. Idempotent; insertion order is preserved.
        """
        self._state._file_glimpsed_state[path] = None

    def get_glimpsed_files(self) -> list[str]:
        """Absolute paths glimpsed via search this session (insertion order)."""
        return list(self._state._file_glimpsed_state.keys())

    def get_revealed_tools(self) -> set[str]:
        """The set of deferred tools revealed (discovered) this session.

        Read live by the executor's tool catalog to decide which deferred
        schemas to expose. Returns the live set (mutated in place by
        :meth:`reveal_tools`).
        """
        return self._state.revealed_tools

    def reveal_tools(self, names: "set[str] | list[str]") -> None:
        """Union *names* into the revealed set (idempotent).

        Callers pass names already validated against the executor's deferred set
        (the Role capability does that intersection), so this method just records
        the discovery; a name revealed twice is a harmless no-op.
        """
        self._state.revealed_tools |= set(names)

    def get_tool_session(self, key: str) -> Any:
        """Return a stateful tool's live session (keyed by tool name), else None."""
        return self._state._tool_sessions.get(key)

    def set_tool_session(self, key: str, value: Any) -> None:
        """Store a stateful tool's live session; a None value clears the slot."""
        if value is None:
            self._state._tool_sessions.pop(key, None)
        else:
            self._state._tool_sessions[key] = value

    def get_pending_terminal_restore(self) -> Optional[dict]:
        """Return the pending terminal-restore state ({cwd, env, unset}), else None."""
        return self._state._pending_terminal_restore

    def set_pending_terminal_restore(self, value: Optional[dict]) -> None:
        """Stage (or clear) the terminal state to restore on next shell start."""
        self._state._pending_terminal_restore = value

    def take_pending_terminal_restore(self) -> Optional[dict]:
        """Return and clear the pending terminal-restore state (consume once).

        Capability surface for the Terminal tool: when it starts a fresh shell it
        consumes the state staged by ``resume_session`` and re-seeds the shell
        once. Reading clears it so the restore happens exactly once.
        """
        value = self._state._pending_terminal_restore
        if value is not None:
            self._state._pending_terminal_restore = None
        return value

    def get_pending_kernel_restore(self) -> Optional[dict]:
        """Return the pending kernel-restore state ({cwd, env, unset}), else None."""
        return self._state._pending_kernel_restore

    def set_pending_kernel_restore(self, value: Optional[dict]) -> None:
        """Stage (or clear) the kernel state to restore on next kernel start."""
        self._state._pending_kernel_restore = value

    def take_pending_kernel_restore(self) -> Optional[dict]:
        """Return and clear the pending kernel-restore state (consume once).

        Capability surface for the Python tool: when it starts a fresh kernel it
        consumes the state staged by ``resume_session`` and re-seeds the kernel
        once. Reading clears it so the restore happens exactly once.
        """
        value = self._state._pending_kernel_restore
        if value is not None:
            self._state._pending_kernel_restore = None
        return value

    def get_pending_browser_restore(self) -> Optional[dict]:
        """Return the pending browser-restore state ({urls, active, storage_state}), else None."""
        return self._state._pending_browser_restore

    def set_pending_browser_restore(self, value: Optional[dict]) -> None:
        """Stage (or clear) the browser state to restore on next browser launch."""
        self._state._pending_browser_restore = value

    def take_pending_browser_restore(self) -> Optional[dict]:
        """Return and clear the pending browser-restore state (consume once).

        Capability surface for the WebBrowser tool: when it launches a fresh
        browser it consumes the state ({urls, active, storage_state}) staged by
        ``resume_session`` and re-opens the saved tabs once. Reading clears it so
        the restore happens exactly once.
        """
        value = self._state._pending_browser_restore
        if value is not None:
            self._state._pending_browser_restore = None
        return value

    def is_active(self) -> bool:
        """Read the react-loop active signal."""
        return self._state._active

    def set_active(self, value: bool) -> None:
        """Write the react-loop active signal."""
        self._state._active = value

    def deactivate(self) -> None:
        """Clear the active signal so the react loop stops after the current step."""
        self._state._active = False

    def put_message(self, message) -> None:
        """Push a message into the private buffer (falsy messages are ignored)."""
        if not message:
            return
        self._state.msg_buffer.push(message)

    @property
    def last_think_result(self) -> "ThinkResult":
        """The most recent think round's output (see ``RoleState.last_think_result``)."""
        return self._state.last_think_result

    def set_last_think_result(self, result: "ThinkResult") -> None:
        """Publish this turn's think result (called by the loop when it drains)."""
        self._state.last_think_result = result

    @property
    def is_idle(self) -> bool:
        """A role is idle when its message buffer is empty."""
        return self._state.msg_buffer.empty()
