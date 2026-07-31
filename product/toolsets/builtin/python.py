"""python — one persistent Python (Jupyter) kernel the model executes code into.

The Python sibling of the persistent :class:`Terminal` tool: there is **one
implicit kernel per Role session** (no kernel id to track, like a notebook), and
the model drives it by executing code:

- ``code`` is run in the kernel. Top-level variables, imports, and defined
  functions persist across calls, so you can build up state step by step.
- ``interrupt=True`` sends a KeyboardInterrupt to a wedged kernel.
- ``restart=True`` rebuilds a clean kernel (clears all state).
- ``close=True`` shuts the kernel down.

Each ``code`` call blocks until the kernel returns to idle (the cell finished)
or ``timeout`` seconds elapse, in which case the kernel is interrupted and the
partial output is returned (its state is preserved). Use this for interactive,
stateful Python work; use the one-shot :class:`Bash` tool for shell commands and
the :class:`Terminal` tool when you need a persistent shell or to drive an
arbitrary interactive program.

The kernel is a managed runtime owned by the Role's ``RuntimeHost``. Calls
acquire serialized, fenced write access; the host owns identity, revision and
teardown.
"""
from __future__ import annotations

import hashlib
import os
from typing import ClassVar

from mote.contracts.artifact import (
    ArtifactPublishRequest,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.contracts.authorization import PermissionDecision
from mote.contracts.runtime import RuntimeAccessMode, RuntimeProjectionIntent
from mote.contracts.runtime.errors import ManagedRuntimeNotFoundError
from mote.product.toolsets.builtin.runtime_action import handoff_permission, is_handoff_action, run_handoff_action
from mote.runtime.errors import ToolError
from mote.runtime.interactive.kernel.driver import DEFAULT_TIMEOUT_S, KernelRuntimeDriver
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import (
    GetArtifactPublisher,
    GetCwd,
    GetRuntimeHost,
    GetSandboxRuntime,
    HandoffRuntime,
)
from mote.runtime.tools.tool_registry import register_tool
from mote.runtime.tools.tool_result import ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_NO_KERNEL_TO_INTERRUPT = "Error: no live kernel to interrupt."
_MSG_KERNEL_FAILED = "Error running Python kernel: {error}"
_MSG_UNKNOWN_ACTION = "Error: unknown Jupyter action '{action}'. Use handoff or leave action empty."
_MSG_EXPORT_FAILED = (
    "Jupyter revision {revision} was committed after the code executed, but its "
    "notebook could not be completed during {stage}: {error}. Do not rerun the "
    "code; call Jupyter with empty code to retry notebook publication."
)
_RUNTIME = "jupyter:default"


@register_tool
class Python(BaseTool):
    """Execute code in a persistent Python kernel (one per session)."""

    name = "Jupyter"
    aliases = ["Python"]
    # Recall synonyms for tool-search: ways a model asks to execute code/cells
    # that the summary ("run Python in a Jupyter kernel") does not spell out.
    keywords: ClassVar[list[str]] = [
        "execute code",
        "notebook",
        "cell",
        "script",
        "compute",
        "calculate",
        "执行代码",
        "运行代码",
        "笔记本",
        "计算",
    ]
    max_result_size_chars: ClassVar[int] = 30_000
    requires = (
        "get_cwd",
        "get_runtime_host",
        "get_sandbox_runtime",
        "get_artifact_publisher",
        "handoff_runtime",
    )
    # Arbitrary code execution.
    risk_level = "high"
    # Fronts a live Jupyter kernel managed by RuntimeHost between calls.
    stateful = True

    # Injected from Role by bind(): cwd seeds the kernel and RuntimeHost owns it.
    get_cwd: GetCwd
    get_runtime_host: GetRuntimeHost
    get_artifact_publisher: GetArtifactPublisher
    handoff_runtime: HandoffRuntime
    # Capability accessor returning the session's SandboxRuntime, or None when no
    # OS-level sandbox is configured. Defaults to a no-runtime stub so a tool
    # bound without a Role (some unit tests) still runs un-sandboxed.
    get_sandbox_runtime: GetSandboxRuntime = staticmethod(lambda: None)

    async def _ensure_runtime(self) -> None:
        """Atomically create this Role's implicit kernel when absent."""
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
            return
        except ManagedRuntimeNotFoundError:
            pass
        cwd = self.get_cwd()
        base_cwd = cwd if cwd and os.path.isdir(cwd) else None
        runtime = self.get_sandbox_runtime() if self.get_sandbox_runtime is not None else None
        driver = KernelRuntimeDriver(
            session_key=self.session_id,
            cwd=base_cwd,
            sandbox_runtime=runtime,
        )
        await host.ensure(driver)

    async def call(
        self,
        *,
        action: str = "",
        code: str = "",
        interrupt: bool = False,
        restart: bool = False,
        close: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,
        message: str = "",
    ) -> str | ToolResult:
        """Run Python in a persistent Jupyter kernel — state persists across calls.

        Execute Python code in a persistent Jupyter kernel kept alive across
        calls (one per session). Variables, imports, and functions persist, so
        you can build up state step by step. Set interrupt=true to send a
        KeyboardInterrupt, restart=true to clear all state, close=true to shut
        the kernel down. Use action=handoff to give the user exclusive control
        of an already-open kernel and wait until control returns.

        Args:
            action: Set to handoff for direct user interaction with the live
                kernel; otherwise leave empty.
            code: Python source to execute in the kernel. State (variables,
                imports, functions) persists across calls. Leave empty when only
                interrupting/restarting/closing.
            interrupt: Send a KeyboardInterrupt to the kernel (e.g. to stop a
                wedged computation). Ignores ``code``.
            restart: Restart the kernel, clearing all in-memory state. Ignores
                ``code``.
            close: Shut the kernel down entirely. Ignores ``code``.
            timeout: Maximum seconds to wait for the code to finish (clamped to
                1..600). On timeout the kernel is interrupted (its state is
                preserved) and whatever it printed so far is returned.
            message: Optional instructions shown to the user during handoff.
        """
        action = (action or "").strip().lower()
        if action == "handoff":
            return await run_handoff_action(self.handoff_runtime, _RUNTIME, message=message)
        if action:
            raise ToolError(_MSG_UNKNOWN_ACTION.format(action=action))
        host = self.get_runtime_host()
        if close:
            try:
                host.descriptor(_RUNTIME)
            except ManagedRuntimeNotFoundError:
                return "[no kernel to close]"
            await host.close(_RUNTIME)
            return "[kernel closed]"

        try:
            if restart:
                try:
                    host.descriptor(_RUNTIME)
                except ManagedRuntimeNotFoundError:
                    await self._ensure_runtime()
                    return "[kernel restarted; all variables cleared]"
                async with host.access(
                    _RUNTIME,
                    mode=RuntimeAccessMode.WRITE,
                    owner_id=f"agent:{self.session_id}:jupyter",
                ) as access:
                    access.commit()
                    driver = access.driver
                    if not isinstance(driver, KernelRuntimeDriver):
                        raise RuntimeError("jupyter runtime has an unexpected driver")
                    await driver.restart()
                return "[kernel restarted; all variables cleared]"
            if interrupt:
                try:
                    host.descriptor(_RUNTIME)
                except ManagedRuntimeNotFoundError:
                    raise ToolError(_MSG_NO_KERNEL_TO_INTERRUPT)
                async with host.access(
                    _RUNTIME,
                    mode=RuntimeAccessMode.WRITE,
                    owner_id=f"agent:{self.session_id}:jupyter",
                ) as access:
                    access.commit()
                    driver = access.driver
                    if not isinstance(driver, KernelRuntimeDriver) or driver.closed:
                        raise ToolError(_MSG_NO_KERNEL_TO_INTERRUPT)
                    text = await driver.interrupt()
                return _join(text, "[kernel interrupted]")
            await self._ensure_runtime()
            access_mode = RuntimeAccessMode.WRITE if code else RuntimeAccessMode.READ
            commit_id: str | None = None
            async with host.access(
                _RUNTIME,
                mode=access_mode,
                owner_id=f"agent:{self.session_id}:jupyter",
            ) as access:
                driver = access.driver
                if not isinstance(driver, KernelRuntimeDriver):
                    raise RuntimeError("jupyter runtime has an unexpected driver")
                if code:
                    text, timed_out = await driver.execute(code, timeout)
                    access.commit(
                        projections=(
                            RuntimeProjectionIntent(
                                intent_id="artifact",
                                projector="notebook-artifact",
                                schema_version=1,
                            ),
                        )
                    )
                else:
                    text, timed_out = "", False
                document = driver.snapshot_document()
            commit_id = access.result_commit_id
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_KERNEL_FAILED.format(error=e))

        revision = host.descriptor(_RUNTIME).revision
        failed_stage = "notebook_export"
        try:
            exports = await driver.export_representations(document)
            representations = tuple(
                ArtifactRepresentationInput(
                    representation=export.representation,
                    kind="notebook",
                    mime_type=export.mime_type,
                    content=export.content,
                    suggested_name=export.suggested_name,
                )
                for export in exports
            )
            notebook_export = next(item for item in representations if item.representation == "ipynb")
            state_digest = hashlib.sha256(notebook_export.content).hexdigest()
            artifact_id = f"notebook-{state_digest}"
            request = ArtifactPublishRequest(
                artifact_id=artifact_id,
                expected_revision=0,
                retention=ArtifactRetention.SESSION,
                sensitivity=ArtifactSensitivity.PRIVATE,
                representations=representations,
            )
            failed_stage = "artifact_publish"
            await self.get_artifact_publisher().publish(
                artifact_id,
                request,
            )
            if commit_id is not None:
                await host.acknowledge_projection(commit_id, "artifact")
        except Exception as exc:  # noqa: BLE001 — committed execution is partial success
            raise ToolError(
                _MSG_EXPORT_FAILED.format(
                    revision=revision,
                    stage=failed_stage,
                    error=exc,
                ),
                cause=exc,
                partial_success=True,
                committed_runtime=_RUNTIME,
                committed_revision=revision,
                failed_stage=failed_stage,
            ) from exc

        if timed_out:
            text = _join(
                text,
                f"[execution timed out after {int(timeout)}s; kernel interrupted " f"— state preserved]",
            )
        return ToolResult(
            output=text,
            data=document,
        )

    def check_permissions(self, args: dict) -> PermissionDecision | None:
        if is_handoff_action(args):
            return handoff_permission()
        return None

    async def cleanup_session(self, session_id: str) -> None:
        """Tear down this Role's kernel (idempotent)."""
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
        except ManagedRuntimeNotFoundError:
            return
        await host.close(_RUNTIME)


def _join(text: str, footer: str) -> str:
    """Join output text with a state footer, dropping empty parts."""
    text = text.rstrip("\n")
    if text:
        return f"{text}\n{footer}"
    return footer
