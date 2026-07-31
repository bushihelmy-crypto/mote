#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the real-tool test suite (``mote.product.toolsets.builtin``).

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
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from mote.contracts.runtime import CheckpointFidelity, RuntimeCheckpoint
from mote.runtime.artifacts import ArtifactRepositoryBlobStore, DurableArtifactStore, ReliableArtifactPublisher
from mote.runtime.interactive import RuntimeHost
from mote.runtime.interactive.checkpoint_codec import decode_inline_json, encode_inline_json
from mote.runtime.session.log import SessionLog
from mote.runtime.tools.base_tool import BaseTool
from mote.ztest.fileops_factory import FileOperations

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
        self._fileops_dir = tempfile.TemporaryDirectory(prefix="mote-tool-test-")
        session_log = SessionLog("tool-test", base_dir=self._fileops_dir.name)
        self.file_operations = FileOperations(
            session_id="tool-test",
            journal_path=session_log.path,
            get_project_root=self.get_cwd,
            flush_pending=session_log.writer.flush_inline,
            lock_root=Path(self._fileops_dir.name) / "locks",
        )
        self.artifact_store = DurableArtifactStore(
            Path(self._fileops_dir.name) / "artifacts.sqlite3",
            ArtifactRepositoryBlobStore(self.file_operations.artifacts.content_repository),
        )
        self.artifact_publisher = ReliableArtifactPublisher(
            self.artifact_store,
            self.artifact_store,
        )
        # Name of the default (main think-loop) model. Media tools (Read/WebBrowser)
        # consult this via get_default_model() to check supports_vision/supports_pdf_input
        # up-front. Defaults to a vision+PDF-capable Claude so media tests read fine;
        # a test can pass a non-vision name (or None) to exercise the refusal path.
        self.default_model = default_model
        # Files glimpsed via a Search match (P2) — recorded, un-read; feeds
        # the code map's navigation view. Insertion order preserved.
        self.glimpsed: list[str] = []
        # ContextVisibility stand-in: what is_resource_visible(path) returns.
        # A bool applies to every path; a callable(path)->bool lets a test script
        # per-file answers (e.g. mark one file's prior read as folded away).
        self.resource_visible = resource_visible
        self.runtime_checkpoints: list[tuple[RuntimeCheckpoint, str]] = []
        self.runtime_host = RuntimeHost(checkpoint_sink=self)
        # Whether the browser applies opt-in stealth (WebBrowser get_browser_stealth).
        self.browser_stealth: bool = False
        # Which locale bundle the stealth fingerprint uses (WebBrowser get_browser_locale).
        self.browser_locale: str = "auto"
        # Optional proxy URL for the browser (WebBrowser get_browser_proxy).
        self.browser_proxy: str = ""
        self.browser_cdp_endpoint: str = ""
        # Durable-login profile name (WebBrowser get_browser_profile). Empty =>
        # ephemeral (no persistence). ``browser_profiles`` is the in-memory
        # encrypted-store stand-in keyed by profile name -> storage_state dict.
        self.browser_profile: str = ""
        self.browser_profiles: dict[str, Optional[dict]] = {}
        # Client TLS certs (WebBrowser get_browser_client_certs) — Playwright
        # ``client_certificates`` shape; a passphrase may be a secret placeholder.
        self.browser_client_certs: list[dict] = []
        # DeviceUse backend config (get_device_config). Defaults to a fresh
        # DeviceConfig ("auto"); a test can reassign to force a backend.
        from mote.runtime.config.device import DeviceConfig

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
        # Names treated as graph orchestrators; run_graph
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

    def capture_file_snapshot(self, path: str, **kwargs):
        return self.file_operations.capture(path, **kwargs)

    def observe_file_snapshot(self, snapshot) -> None:
        self.file_operations.observe(snapshot)

    def get_file_snapshot(self, path: str):
        return self.file_operations.observed(path)

    def read_file_view(self, path: str, request):
        return self.file_operations.read_view(path, request)

    def search_files(self, **kwargs):
        return self.file_operations.search(**kwargs)

    async def plan_file_edit(self, request):
        return self.file_operations.plan_file_edit(request)

    async def commit_edit_plan(self, plan_id: str, **kwargs):
        return self.file_operations.commit_edit_plan(plan_id, **kwargs)

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

    async def persist(self, checkpoint: RuntimeCheckpoint, *, reason: str) -> None:
        self.runtime_checkpoints.append((checkpoint, reason))

    def latest_runtime_state(self, kind: str, codec: str) -> dict:
        checkpoint = next(
            checkpoint for checkpoint, _reason in reversed(self.runtime_checkpoints) if checkpoint.kind == kind
        )
        return decode_inline_json(checkpoint, codec=codec)

    def stage_runtime_checkpoint(self, kind: str, codec: str, payload: dict) -> None:
        encoded = encode_inline_json(payload, codec=codec, fidelity=CheckpointFidelity.LOGICAL)
        self.runtime_host.stage_checkpoint(
            RuntimeCheckpoint(
                runtime_id=f"tool-test-{kind}",
                kind=kind,
                epoch=0,
                revision=0,
                codec=encoded.codec,
                schema_version=encoded.schema_version,
                payload_ref=encoded.payload_ref,
                digest=encoded.digest,
                fidelity=encoded.fidelity or CheckpointFidelity.LOGICAL,
            )
        )

    def get_browser_stealth(self) -> bool:
        return self.browser_stealth

    def get_browser_locale(self) -> str:
        return self.browser_locale

    def get_browser_proxy(self) -> str:
        return self.browser_proxy

    def get_browser_cdp_endpoint(self) -> str:
        return self.browser_cdp_endpoint

    # --- durable-login profile (WebBrowser seeds from / persists to it) ---
    def get_browser_profile(self) -> str:
        return self.browser_profile

    def load_browser_profile(self, name: str) -> Optional[dict]:
        return self.browser_profiles.get(name)

    def save_browser_profile(self, name: str, storage_state: Optional[dict]) -> None:
        self.browser_profiles[name] = storage_state

    def get_browser_client_certs(self) -> list[dict]:
        return [dict(c) for c in self.browser_client_certs]

    # --- named-secret resolution (autonomous login-fill) ---
    def get_secret(self, key: str) -> Optional[str]:
        return self.secrets.get(key) or None

    def get_runtime_host(self) -> RuntimeHost:
        return self.runtime_host

    def get_artifact_publisher(self) -> ReliableArtifactPublisher:
        return self.artifact_publisher

    # --- human / session ---
    async def ask_user(self, question: str) -> str:
        self.ask_questions.append(question)
        return self.ask_reply

    async def ask_user_question(self, questions):
        """Structured AskUserQuestion channel — records items, returns scripted answers.

        ``ask_answers`` may be a callable(items) -> AskUserQuestionAnswers or a
        pre-built AskUserQuestionAnswers; absent it returns empty answers.
        """
        from mote.contracts.interaction import AskUserQuestionAnswers

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
        from mote.runtime.tools.tool_result import ToolResult

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
        from mote.contracts.output import CommittedOutput, OutputDecodeError, RunKind
        from mote.kernel.output import JsonSchemaOutputDecoder

        decoder = JsonSchemaOutputDecoder(contract_spec.schema_)
        try:
            value = decoder.decode(output)
        except OutputDecodeError:
            from mote.runtime.errors import GraphError

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

    # --- vision fallback (WebBrowser read_image's secondary call) ---
    async def describe_image(self, image_b64: str, **kwargs) -> str:
        self.describe_image_calls.append((image_b64, kwargs))
        if self.describe_image_text is None:
            raise NotImplementedError("no vision-capable model")
        return self.describe_image_text

    async def handoff_runtime(self, runtime: str, *, message: str = ""):
        raise AssertionError(f"unexpected handoff of {runtime!r} in tool test: {message!r}")

    # --- the allowlist bind() consults ---
    def tool_capabilities(self) -> dict[str, Any]:
        return {
            "get_cwd": self.get_cwd,
            "set_cwd": self.set_cwd,
            "get_default_model": self.get_default_model,
            "capture_file_snapshot": self.capture_file_snapshot,
            "observe_file_snapshot": self.observe_file_snapshot,
            "read_file_view": self.read_file_view,
            "search_files": self.search_files,
            "plan_file_edit": self.plan_file_edit,
            "commit_edit_plan": self.commit_edit_plan,
            "record_file_glimpsed": self.record_file_glimpsed,
            "is_resource_visible": self.is_resource_visible,
            "get_browser_stealth": self.get_browser_stealth,
            "get_browser_locale": self.get_browser_locale,
            "get_browser_proxy": self.get_browser_proxy,
            "get_browser_cdp_endpoint": self.get_browser_cdp_endpoint,
            "get_browser_profile": self.get_browser_profile,
            "load_browser_profile": self.load_browser_profile,
            "save_browser_profile": self.save_browser_profile,
            "get_browser_client_certs": self.get_browser_client_certs,
            "get_secret": self.get_secret,
            "get_runtime_host": self.get_runtime_host,
            "handoff_runtime": self.handoff_runtime,
            "get_artifact_publisher": self.get_artifact_publisher,
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
    """Record the exact sealed version of *full_path* as observed."""
    snapshot, _ = role.capture_file_snapshot(full_path, encoding="utf-8")
    role.observe_file_snapshot(snapshot)
