#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the real-tool test suite (``mote.executor.tools``).

Everything stays fully offline and deterministic — no LLM, no network, no MCP.
The real tools only ever touch:

- the local filesystem (Read/Write/Edit/Grep/Glob/Bash), which we
  point at a per-test ``tmp_path`` workspace and ``chdir`` into so relative
  paths and ``os.getcwd()`` are predictable;
- a handful of narrow Role capabilities (cwd accessors, the shared file-read
  state, ``wait_interruptible``, ``end_session``, ``ask_user``,
  ``reply_to_user``), all provided here by ``CapRole`` — a configurable fake
  Role that publishes an explicit ``tool_capabilities()`` allowlist exactly like
  the real Role, so ``BaseTool.bind`` injects only what each tool declares.

Helpers:
- ``CapRole`` — fake Role; tracks cwd + an in-memory file-read-state dict, lets
  tests script ``ask_user``/``end_session`` replies.
- ``bind`` — bind a tool to a session + role (returns the tool).
- ``run`` — drive a (bound) tool's ``async call(**kwargs)`` to completion.
- ``workspace`` — a tmp dir the test cwd is switched into.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import pytest

from mote.executor.base_tool import BaseTool

# ---------------------------------------------------------------------------
# Fake Role publishing a capability allowlist
# ---------------------------------------------------------------------------


class CapRole:
    """Minimal Role stand-in exposing the capabilities the real tools require.

    Each capability is a plain callable kept on the instance so tests can read
    back what a tool did (cwd writes, recorded reads) or script replies. Only
    the names listed in ``tool_capabilities()`` are visible to ``bind`` — a tool
    can never reach anything else.
    """

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        ask_reply: str = "",
        end_output: str = "session ended",
        wait_result: Optional[float] = None,
        sandbox_runtime: Any = None,
        resource_visible: Any = True,
        default_model: Optional[str] = "claude-sonnet-4",
    ) -> None:
        self._cwd = cwd or os.getcwd()
        # Name of the default (main think-loop) model. Media tools (Read/WebBrowser)
        # consult this via get_default_model() to check supports_vision/supports_pdf_input
        # up-front. Defaults to a vision+PDF-capable Claude so media tests read fine;
        # a test can pass a non-vision name (or None) to exercise the refusal path.
        self.default_model = default_model
        # Shared file-read state: full_path -> mtime_ns (the real Role's readFileState).
        self.read_state: dict[str, int] = {}
        # Files glimpsed via a Search match (P2) — recorded, un-read; feeds
        # the code map's navigation view. Insertion order preserved.
        self.glimpsed: list[str] = []
        # ContextVisibility stand-in: what is_resource_visible(path) returns.
        # A bool applies to every path; a callable(path)->bool lets a test script
        # per-file answers (e.g. mark one file's prior read as folded away).
        self.resource_visible = resource_visible
        # Per-Role live sessions for stateful tools (terminal/kernel), keyed by
        # tool name. Mirrors RoleState._tool_sessions — owned by this fake Role,
        # so each test's tools are isolated (no process-global leakage).
        self.tool_sessions: dict[str, Any] = {}
        # Before-image snapshot calls: (full_path, tool) recorded for assertions.
        self.snapshots: list[tuple[str, str]] = []
        # External-change attribution: known baselines (path -> content) recorded
        # by record_file_baseline; paths passed to attribute_external_change.
        self.baselines: dict[str, str] = {}
        self.external_attributions: list[str] = []
        # Persistent-terminal state captures: (cwd, env, unset, tool) recorded.
        self.terminal_states: list[tuple] = []
        # Persistent-kernel state captures: (cwd, env, unset, tool) recorded.
        self.kernel_states: list[tuple] = []
        # Persistent-browser state captures: (urls, active, storage_state, tool).
        self.browser_states: list[tuple] = []
        # Pending terminal-restore state ({cwd, env, unset}) staged by a resume;
        # consumed once via take_pending_terminal_restore().
        self._pending_restore: Optional[dict] = None
        # Pending kernel-restore state ({cwd, env, unset}) staged by a resume;
        # consumed once via take_pending_kernel_restore().
        self._pending_kernel_restore: Optional[dict] = None
        # Pending browser-restore state ({urls, active, storage_state}) staged by
        # a resume; consumed once via take_pending_browser_restore().
        self._pending_browser_restore: Optional[dict] = None
        # Whether the browser launches headless (WebBrowser get_browser_headless).
        self.browser_headless: bool = True
        # Whether the browser applies opt-in stealth (WebBrowser get_browser_stealth).
        self.browser_stealth: bool = False
        # Which locale bundle the stealth fingerprint uses (WebBrowser get_browser_locale).
        self.browser_locale: str = "auto"
        # Optional proxy URL for the browser (WebBrowser get_browser_proxy).
        self.browser_proxy: str = ""
        # Durable-login profile name (WebBrowser get_browser_profile). Empty =>
        # ephemeral (no persistence). ``browser_profiles`` is the in-memory
        # encrypted-store stand-in keyed by profile name -> storage_state dict.
        self.browser_profile: str = ""
        self.browser_profiles: dict[str, Optional[dict]] = {}
        # Client TLS certs (WebBrowser get_browser_client_certs) — Playwright
        # ``client_certificates`` shape; a passphrase may be a secret placeholder.
        self.browser_client_certs: list[dict] = []
        # CDP endpoint (WebBrowser get_browser_cdp_endpoint). Empty => launch.
        self.browser_cdp_endpoint: str = ""
        # DeviceUse backend config (get_device_config). Defaults to a fresh
        # DeviceConfig ("auto"); a test can reassign to force a backend.
        from mote.common.schema import DeviceConfig

        self.device_config: Any = DeviceConfig()
        # Named-secret vault stand-in for autonomous login-fill (get_secret).
        self.secrets: dict[str, str] = {}
        self.graph_resume_result = None
        self.graph_resume_calls: list[tuple[Any, str]] = []
        # Scriptable human/session behaviour.
        self.ask_reply = ask_reply
        self.ask_questions: list[str] = []  # records every prompt sent to ask_user
        # AskUserQuestion structured channel: a callable(items) -> AskUserQuestionAnswers
        # (or a pre-built AskUserQuestionAnswers). ``ask_question_items`` records
        # the typed questions each call received.
        self.ask_answers: Any = None
        self.ask_question_items: list = []
        self.end_output = end_output
        self.end_calls = 0
        # slept_seconds returned by wait_interruptible; defaults to 0.0.
        self._wait_result = wait_result
        # Records each `duration` arg wait_interruptible was called with, so a
        # test can assert the durable-timer Sleep threaded its bound through.
        self.wait_durations: list[Optional[float]] = []
        # Optional OS-level sandbox runtime for the command-execution tools.
        # None => those tools run un-sandboxed (the historical test behavior).
        self._sandbox_runtime = sandbox_runtime
        # Scriptable tool table for the run_graph orchestrator: name -> async
        # fn(kwargs) -> ToolResult. ``dispatch_tool`` routes here (the fake
        # executor chokepoint) and ``list_tool_names`` reports the keys, so a
        # graph node can call these fakes exactly as it would a real tool.
        self.fake_tools: dict[str, Callable] = {}
        # Names treated as graph orchestrators (``is_graph_tool``); run_graph
        # refuses to nest these. Empty unless a test opts in.
        self.graph_tools: set[str] = set()
        # Names that must not appear as a graph node (``graph_excluded``, e.g.
        # Sleep); run_graph refuses to reference these. Empty unless a test opts in.
        self.excluded_tools: set[str] = set()
        # Tool-search: the deferred-tool menu (name -> one-line desc) SearchTools
        # searches over, plus the revealed set it unions into. Empty unless a
        # test opts in.
        self.deferred_index: dict[str, str] = {}
        self.revealed: set[str] = set()
        # Full descriptions SearchTools reads on reveal to persist (defaults to
        # the one-line ``deferred_index`` text unless a test sets richer prose).
        self.deferred_descriptions: dict[str, str] = {}
        # Resources SearchTools/Skill register on reveal (id -> (kind, content)),
        # captured so a test can assert the persisted descriptions.
        self.registered_resources: dict[str, tuple[str, str]] = {}
        # Server-side web search (WebSearch tool). ``web_search_hits`` is the
        # scripted result list; when it is None the capability raises
        # NotImplementedError (models the "server-side search unavailable" path).
        # ``web_search_calls`` records each (query, kwargs) for assertions.
        self.web_search_hits: Any = None
        self.web_search_calls: list[tuple] = []
        # Vision fallback (WebBrowser read_image). ``describe_image_text`` is the
        # scripted description; when it is None the capability raises
        # NotImplementedError (models the "no vision-capable model" path).
        # ``describe_image_calls`` records each (image_b64, kwargs) for assertions.
        self.describe_image_text: Any = None
        self.describe_image_calls: list[tuple] = []

    # --- cwd accessors (Bash) ---
    def get_cwd(self) -> str:
        return self._cwd

    def set_cwd(self, path: str) -> None:
        self._cwd = path

    def get_default_model(self) -> Optional[str]:
        return self.default_model

    # --- shared file-read state (Read records, Write/Edit enforce) ---
    def record_file_read(self, path: str, mtime_ns: int) -> None:
        self.read_state[path] = mtime_ns

    def get_file_read_mtime(self, path: str) -> Optional[int]:
        return self.read_state.get(path)

    # --- glimpse state (Search records matched files for the code map) ---
    def record_file_glimpsed(self, path: str) -> None:
        if path not in self.glimpsed:
            self.glimpsed.append(path)

    # --- context visibility (Read consults before returning a dedup stub) ---
    def is_resource_visible(self, path: str) -> bool:
        vis = self.resource_visible
        if callable(vis):
            return bool(vis(path))
        return bool(vis)

    # --- file-history snapshot (Write/Edit capture before-images) ---
    def record_file_snapshot(self, full_path: str, *, tool: str = "") -> None:
        self.snapshots.append((full_path, tool))

    # --- external-change attribution (Write/Edit ledger out-of-band edits) ---
    def record_file_baseline(self, full_path: str) -> None:
        # Store the just-written content as mote's known baseline (path -> content).
        try:
            self.baselines[full_path] = open(full_path, encoding="utf-8").read()
        except OSError:
            pass

    def attribute_external_change(self, full_path: str) -> None:
        # Record the guard's attribution call so a test can assert it fired
        # before the write was refused (attribution-then-guard).
        self.external_attributions.append(full_path)

    # --- persistent-terminal state (Terminal captures cwd+env for resume) ---
    def record_terminal_state(self, cwd, env, unset, *, tool: str = "") -> None:
        self.terminal_states.append((cwd, env, unset, tool))

    def take_pending_terminal_restore(self) -> Optional[dict]:
        value = self._pending_restore
        self._pending_restore = None
        return value

    # --- persistent-kernel state (Python captures cwd+env for resume) ---
    def record_kernel_state(self, cwd, env, unset, *, tool: str = "") -> None:
        self.kernel_states.append((cwd, env, unset, tool))

    def take_pending_kernel_restore(self) -> Optional[dict]:
        value = self._pending_kernel_restore
        self._pending_kernel_restore = None
        return value

    # --- persistent-browser state (WebBrowser captures tabs+session for resume) ---
    def record_browser_state(self, urls, *, active=0, storage_state=None, tool: str = "") -> None:
        self.browser_states.append((urls, active, storage_state, tool))

    def take_pending_browser_restore(self) -> Optional[dict]:
        value = self._pending_browser_restore
        self._pending_browser_restore = None
        return value

    def get_browser_headless(self) -> bool:
        return self.browser_headless

    def get_browser_stealth(self) -> bool:
        return self.browser_stealth

    def get_browser_locale(self) -> str:
        return self.browser_locale

    def get_browser_proxy(self) -> str:
        return self.browser_proxy

    # --- durable-login profile (WebBrowser seeds from / persists to it) ---
    def get_browser_profile(self) -> str:
        return self.browser_profile

    def load_browser_profile(self, name: str) -> Optional[dict]:
        return self.browser_profiles.get(name)

    def save_browser_profile(self, name: str, storage_state: Optional[dict]) -> None:
        self.browser_profiles[name] = storage_state

    def get_browser_client_certs(self) -> list[dict]:
        return [dict(c) for c in self.browser_client_certs]

    def get_browser_cdp_endpoint(self) -> str:
        return self.browser_cdp_endpoint

    # --- named-secret resolution (autonomous login-fill) ---
    def get_secret(self, key: str) -> Optional[str]:
        return self.secrets.get(key) or None

    # --- stateful-tool sessions (Terminal/Python live state on RoleState) ---
    def get_tool_session(self, key: str) -> Any:
        return self.tool_sessions.get(key)

    def set_tool_session(self, key: str, value: Any) -> None:
        if value is None:
            self.tool_sessions.pop(key, None)
        else:
            self.tool_sessions[key] = value

    # --- human / session ---
    async def ask_user(self, question: str) -> str:
        self.ask_questions.append(question)
        return self.ask_reply

    async def ask_user_question(self, questions):
        """Structured AskUserQuestion channel — records items, returns scripted answers.

        ``ask_answers`` may be a callable(items) -> AskUserQuestionAnswers or a
        pre-built AskUserQuestionAnswers; absent it returns empty answers.
        """
        from mote.common.schema import AskUserQuestionAnswers

        self.ask_question_items.append(questions)
        answers = self.ask_answers
        if callable(answers):
            return answers(questions)
        if answers is not None:
            return answers
        return AskUserQuestionAnswers()

    async def reply_to_user(self, content: str) -> str:
        return content

    async def end_session(self) -> str:
        self.end_calls += 1
        return self.end_output

    # --- sleep ---
    async def wait_interruptible(self, duration: Optional[float] = None) -> float:
        self.wait_durations.append(duration)
        if self._wait_result is not None:
            return self._wait_result
        return 0.0

    # --- OS-level sandbox runtime (Bash/terminal/python) ---
    def get_sandbox_runtime(self) -> Any:
        return self._sandbox_runtime

    # --- DeviceUse backend config ---
    def get_device_config(self) -> Any:
        return self.device_config

    # --- run_graph orchestration (fake executor chokepoint) ---
    async def dispatch_tool(self, name: str, kwargs: Optional[dict] = None) -> Any:
        from mote.executor.tool_result import ToolResult

        fn = self.fake_tools.get(name)
        if fn is None:
            return ToolResult(output=f"no such tool: {name}", success=False)
        return await fn(kwargs or {})

    def list_tool_names(self) -> list[str]:
        return list(self.fake_tools)

    def list_graph_tool_names(self) -> list[str]:
        # Names in ``self.graph_tools`` are treated as graph orchestrators (so a
        # test can prove run_graph refuses to nest them). Empty by default.
        return list(getattr(self, "graph_tools", ()) or ())

    def list_graph_excluded_tool_names(self) -> list[str]:
        # Names in ``self.excluded_tools`` must not appear as a graph node (e.g.
        # Sleep). Lets a test prove run_graph refuses to reference them. Empty by
        # default.
        return list(getattr(self, "excluded_tools", ()) or ())

    async def commit_graph_output(self, *, output, contract_spec, run_id):
        from mote.common.schema import CommittedOutput, OutputDecodeError, RunKind
        from mote.roles.output_contract import JsonSchemaOutputDecoder

        decoder = JsonSchemaOutputDecoder(contract_spec.schema_)
        try:
            value = decoder.decode(output)
        except OutputDecodeError:
            from mote.common.exception import GraphError

            raise GraphError("Graph terminal output did not satisfy its output contract")
        return CommittedOutput(
            candidate_id="graph-candidate",
            contract_id=(f"{contract_spec.namespace}.{contract_spec.name}" f"@{contract_spec.version}"),
            schema_fingerprint=decoder.schema.fingerprint,
            value=value,
            run_id=run_id,
            run_kind=RunKind.GRAPH,
        )

    async def resume_graph_output(self, *, contract_spec, run_id):
        self.graph_resume_calls.append((contract_spec, run_id))
        return self.graph_resume_result

    def has_graph_output_restore(self, run_id: str) -> bool:
        return self.graph_resume_result is not None

    @asynccontextmanager
    async def graph_run_lease(self, run_id: str):
        yield

    # --- tool-search (SearchTools discovers + reveals deferred tools) ---
    def list_deferred_tools(self) -> dict[str, str]:
        return dict(self.deferred_index)

    def reveal_tools(self, names: list[str]) -> list[str]:
        accepted = [n for n in names if n in self.deferred_index]
        self.revealed |= set(accepted)
        return accepted

    def describe_deferred_tools(self, names: list[str]) -> dict[str, str]:
        # Full description if a test provided one, else the one-line index text.
        return {
            n: self.deferred_descriptions.get(n, self.deferred_index.get(n, ""))
            for n in names
            if n in self.deferred_index
        }

    def register_resource(self, *, id: str, kind: str, content: str) -> None:
        self.registered_resources[id] = (kind, content)

    # --- server-side web search (WebSearch tool's secondary call) ---
    async def web_search(self, query: str, **kwargs) -> Any:
        self.web_search_calls.append((query, kwargs))
        if self.web_search_hits is None:
            raise NotImplementedError("no server-side web search")
        return self.web_search_hits

    # --- vision fallback (WebBrowser read_image's secondary call) ---
    async def describe_image(self, image_b64: str, **kwargs) -> str:
        self.describe_image_calls.append((image_b64, kwargs))
        if self.describe_image_text is None:
            raise NotImplementedError("no vision-capable model")
        return self.describe_image_text

    # --- the allowlist bind() consults ---
    def tool_capabilities(self) -> dict[str, Any]:
        return {
            "get_cwd": self.get_cwd,
            "set_cwd": self.set_cwd,
            "get_default_model": self.get_default_model,
            "record_file_read": self.record_file_read,
            "get_file_read_mtime": self.get_file_read_mtime,
            "record_file_glimpsed": self.record_file_glimpsed,
            "is_resource_visible": self.is_resource_visible,
            "record_file_snapshot": self.record_file_snapshot,
            "record_file_baseline": self.record_file_baseline,
            "attribute_external_change": self.attribute_external_change,
            "record_terminal_state": self.record_terminal_state,
            "take_pending_terminal_restore": self.take_pending_terminal_restore,
            "record_kernel_state": self.record_kernel_state,
            "take_pending_kernel_restore": self.take_pending_kernel_restore,
            "record_browser_state": self.record_browser_state,
            "take_pending_browser_restore": self.take_pending_browser_restore,
            "get_browser_headless": self.get_browser_headless,
            "get_browser_stealth": self.get_browser_stealth,
            "get_browser_locale": self.get_browser_locale,
            "get_browser_proxy": self.get_browser_proxy,
            "get_browser_profile": self.get_browser_profile,
            "load_browser_profile": self.load_browser_profile,
            "save_browser_profile": self.save_browser_profile,
            "get_browser_client_certs": self.get_browser_client_certs,
            "get_browser_cdp_endpoint": self.get_browser_cdp_endpoint,
            "get_secret": self.get_secret,
            "get_tool_session": self.get_tool_session,
            "set_tool_session": self.set_tool_session,
            "ask_user": self.ask_user,
            "ask_user_question": self.ask_user_question,
            "reply_to_user": self.reply_to_user,
            "end_session": self.end_session,
            "wait_interruptible": self.wait_interruptible,
            "get_sandbox_runtime": self.get_sandbox_runtime,
            "get_device_config": self.get_device_config,
            "dispatch_tool": self.dispatch_tool,
            "list_tool_names": self.list_tool_names,
            "list_graph_tool_names": self.list_graph_tool_names,
            "list_graph_excluded_tool_names": self.list_graph_excluded_tool_names,
            "commit_graph_output": self.commit_graph_output,
            "resume_graph_output": self.resume_graph_output,
            "has_graph_output_restore": self.has_graph_output_restore,
            "graph_run_lease": self.graph_run_lease,
            "list_deferred_tools": self.list_deferred_tools,
            "reveal_tools": self.reveal_tools,
            "describe_deferred_tools": self.describe_deferred_tools,
            "register_resource": self.register_resource,
            "web_search": self.web_search,
            "describe_image": self.describe_image,
        }


# ---------------------------------------------------------------------------
# Binding / driving helpers
# ---------------------------------------------------------------------------


def bind(tool: BaseTool, role: Optional[CapRole] = None, session_id: str = "sess") -> BaseTool:
    """Bind a tool to a session (+ optional role) and return it."""
    return tool.bind(session_id, role=role)


# One event loop shared by every ``run()`` call for the whole test session.
#
# Why not a fresh loop per call (or :func:`asyncio.run`)? Playwright's Chromium
# child is an ``asyncio.subprocess`` whose ``BaseSubprocessTransport`` is not
# always reclaimed synchronously when a scenario ends — it lingers until a LATER
# garbage collection. If the loop that created it has since been closed, the
# transport's ``__del__`` calls ``loop.call_soon`` on that closed loop and raises
# ``RuntimeError: Event loop is closed`` — a stray unraisable that flakily fails
# whichever unrelated test the collector happened to run in. Keeping a single
# loop open for the entire session means no transport is ever finalised against a
# closed loop; the loop is closed once, at process exit, after a final drain.
_SHARED_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _get_shared_loop() -> asyncio.AbstractEventLoop:
    global _SHARED_LOOP
    if _SHARED_LOOP is None or _SHARED_LOOP.is_closed():
        _SHARED_LOOP = asyncio.new_event_loop()

        import atexit
        import gc

        def _drain_and_close(loop: asyncio.AbstractEventLoop = _SHARED_LOOP) -> None:
            # Reclaim any lingering transports and pump their finalisers onto the
            # still-open loop before closing it, so nothing fires against a
            # closed loop after the process starts tearing down.
            try:
                gc.collect()
                loop.run_until_complete(asyncio.sleep(0))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            finally:
                loop.close()

        atexit.register(_drain_and_close)
    return _SHARED_LOOP


def run(coro):
    """Drive a coroutine (a tool's ``call``) to completion synchronously.

    Runs on a single session-wide event loop (see ``_get_shared_loop``) so a
    lazily-GC'd Playwright transport never fires its finaliser against a closed
    loop. A per-call ``gc.collect()`` + one loop turn eagerly drains finalisers
    while the loop is live, keeping late collections quiet.
    """
    import gc

    loop = _get_shared_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # Drain finalisers onto the still-open shared loop each call, so lingering
        # transports are reclaimed here rather than during a later, unrelated test.
        gc.collect()
        loop.run_until_complete(asyncio.sleep(0))


# ---------------------------------------------------------------------------
# Filesystem fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A tmp directory the process cwd is switched into for the test.

    Tools resolve relative paths against ``os.getcwd()`` and relativize output
    against it, so anchoring the cwd makes those deterministic.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def role():
    """A fresh CapRole whose cwd starts at the current working directory."""
    return CapRole()


def write_file(path, content: str, *, newline: str = "") -> str:
    """Write *content* to *path* (str/Path), return the absolute path.

    ``newline=""`` disables translation so callers control byte-exact endings.
    """
    full = os.path.abspath(os.path.expanduser(str(path)))
    with open(full, "w", encoding="utf-8", newline=newline) as f:
        f.write(content)
    return full


def mark_read(role: CapRole, full_path: str) -> None:
    """Record *full_path* in the role's file-read state at its current mtime.

    Mirrors what a successful Read does, so a Write/Edit guard sees the file as
    "read this session and unchanged since".
    """
    role.record_file_read(full_path, os.stat(full_path).st_mtime_ns)
