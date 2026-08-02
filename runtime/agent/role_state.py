#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RoleState — serializable runtime snapshot for checkpoint/recovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from mote.contracts.conversation import LLMCallContext, Message, MessageQueue
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.routing import RoutingSessionState
from mote.kernel.execution.run_state import AgentRunState
from mote.runtime.session.ids import new_session_id


class RoleState(BaseModel):
    """Serializable runtime snapshot, used for cross-machine restoration."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

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
    working_dir: str = Field(default_factory=lambda: str(Path.cwd().resolve()))
    original_working_dir: str = Field(default_factory=lambda: str(Path.cwd().resolve()))
    project_root: str = Field(default_factory=lambda: str(Path.cwd().resolve()))

    # Execution tracking
    latest_observed_msg: Optional[Message] = None
    recovered: bool = False
    # Kernel execution signals are intentionally excluded from checkpoints. A
    # recovered Runtime starts a fresh Flow over the durable session state.
    run_state: AgentRunState = Field(default_factory=AgentRunState, exclude=True)

    # Routing
    addresses: set[str] = set()
    watch: set[str] = Field(default_factory=set)
    routing: RoutingSessionState = Field(default_factory=RoutingSessionState)

    # Tool-search: the set of deferred tools the model has discovered (revealed)
    # this session. Serialized so a resumed session keeps its discovered tools
    # visible (rides the checkpoint like ``addresses``). The executor's tool
    # catalog reads it via a live getter to decide which deferred schemas to
    # withhold; the ``RoleStateController`` owns the validated union.
    revealed_tools: set[str] = Field(default_factory=set)

    # Hunk attribution: the current turn (prompt) index — a monotonic counter the
    # react loop advances once per think round. Change hunks captured during a
    # turn are stamped with this value so the review layer can group "pending
    # changes by turn" and attribute each agent edit to the turn that made it.
    # Serialized so a resumed session keeps counting from where it left off
    # (rides the checkpoint like ``revealed_tools``); the ``RoleStateController``
    # owns the advance.
    turn_index: int = 0

    # Files the session merely *glimpsed* — surfaced by a Search match but not
    # read in full. Distinct from FileOperations' observed sealed snapshots; a glimpse carries no body,
    # so it feeds only the code map's navigation view (defines + intent to help
    # the model choose what to Read), never the read-before-write guard or the
    # code map's F1 in-context suppression. An ordered set (dict for insertion
    # order); paths are absolute and runtime-only.
    _file_glimpsed_state: dict[str, None] = PrivateAttr(default_factory=dict)

    # Unfinished typed-output lifecycle folded from rollout.jsonl. Consumed once
    # by the next loop factory; published outputs are never staged here.
    _pending_output_restore: Optional[dict] = PrivateAttr(default=None)
    _pending_graph_output_restores: dict[str, dict] = PrivateAttr(default_factory=dict)


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

    def current_turn_index(self) -> int:
        """The current turn (prompt) index — the value hunks are attributed to.

        Read live by the hunk-attribution path (at executor settle and in the
        read-before-write guard) to stamp each captured hunk with the turn that
        produced it. Monotonic, advanced by :meth:`advance_turn`.
        """
        return self._state.turn_index

    def advance_turn(self) -> int:
        """Increment and return the turn index (called once per react think round)."""
        self._state.turn_index += 1
        return self._state.turn_index

    def set_pending_output_restore(self, value: Optional[dict]) -> None:
        """Stage (or clear) one unfinished durable output lifecycle."""
        self._state._pending_output_restore = value

    def take_pending_output_restore(self) -> Optional[dict]:
        """Consume the unfinished durable output lifecycle exactly once."""
        value = self._state._pending_output_restore
        self._state._pending_output_restore = None
        return value

    def get_pending_output_restore(self) -> Optional[dict]:
        """Inspect the staged lifecycle without consuming it."""
        return self._state._pending_output_restore

    def set_pending_graph_output_restores(self, values: dict[str, dict]) -> None:
        self._state._pending_graph_output_restores = dict(values)

    def take_pending_graph_output_restore(self, run_id: str) -> Optional[dict]:
        return self._state._pending_graph_output_restores.pop(run_id, None)

    def has_pending_graph_output_restore(self, run_id: str) -> bool:
        state = self._state._pending_graph_output_restores.get(run_id)
        return state is not None and state.get("status") in {
            "accepted",
            "commit_started",
            "committed",
            "publication_queued",
        }

    def is_active(self) -> bool:
        """Read the react-loop active signal."""
        return self._state.run_state.active

    def set_active(self, value: bool) -> None:
        """Write the react-loop active signal."""
        self._state.run_state.active = value

    def deactivate(self) -> None:
        """Clear the active signal so the react loop stops after the current step."""
        self._state.run_state.active = False

    def put_message(self, message) -> None:
        """Push a message into the private buffer (falsy messages are ignored)."""
        if not message:
            return
        self._state.msg_buffer.push(message)

    @property
    def last_inference_result(self) -> "InferenceResult":
        """The most recent think round's transient Kernel output."""
        return self._state.run_state.last_inference_result

    def set_last_think_result(self, result: "InferenceResult") -> None:
        """Publish this turn's think result (called by the flow when it drains)."""
        self._state.run_state.last_inference_result = result

    @property
    def is_idle(self) -> bool:
        """A role is idle when its message buffer is empty."""
        return self._state.msg_buffer.empty()
