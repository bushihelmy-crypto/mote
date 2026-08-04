#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the persistent Python-kernel ``python`` tool.

Drives a real ipykernel through the tool's ``call``, using the shared
``CapRole``/``bind``/``run``/``workspace`` harness. Everything is local and
offline.

A live kernel keeps its client channels on the event loop it was started on, so
multi-call scenarios run inside ONE ``asyncio.run`` (the conftest ``run`` opens a
fresh loop per call). The live session is owned by the per-test ``CapRole``
through its ``RuntimeHost``, so there is no process-global singleton to leak
across tests; each test still tears its kernel down to free the subprocess.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from mote.contracts.interaction.handoff import HandoffRequest, HandoffStatus, HumanHandoffOutcome
from mote.contracts.runtime import RuntimeRef, RuntimeState
from mote.contracts.surface import NOTEBOOK_MEDIA_TYPE, NotebookDocument, SurfaceInput, SurfacePresentationMode
from mote.contracts.tool.errors import ToolError
from mote.product.toolsets.builtin.python import Python
from mote.runtime.interactive.kernel.driver import OUTPUT_MAX_CHARS, KernelRuntimeDriver, KernelSession, _strip_ansi
from mote.runtime.text.elision import cap_head_tail
from mote.runtime.tools.tool_result import ToolResult

from .conftest import CapRole, bind, run


def _has_kernel(role: CapRole) -> bool:
    """Whether the fake Role owns a live managed Jupyter runtime."""
    return any(item.ref.readable == "jupyter:default" for item in role.runtime_host.list())


@pytest.fixture
def caprole(workspace):
    return CapRole(cwd=str(workspace))


class FailingArtifactPublisher:
    async def publish(self, publication_id, request):
        raise OSError("artifact index unavailable")


# ---------------------------------------------------------------------------
# execute — output + persistent state
# ---------------------------------------------------------------------------


class TestExecute:
    def test_agent_input_request_fails_without_waiting_for_human(self, caprole):
        tool = bind(Python(), caprole, session_id="k_no_stdin")

        async def scenario():
            result = await asyncio.wait_for(tool.call(code="input('blocked: ')"), 10)
            assert isinstance(result, ToolResult)
            assert "StdinNotImplementedError" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_execution_keeps_internal_notebook_artifact_out_of_result(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_artifact")

        async def scenario():
            result = await tool.call(code="answer = 40 + 2\nprint(answer)")
            assert isinstance(result, ToolResult)
            assert result.media == []
            assert result.artifacts == []
            assert "artifact:" not in result.output
            assert "42" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_empty_code_observes_and_republishes_without_new_cell(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_observe")

        async def scenario():
            executed = await tool.call(code="value = 7")
            observed = await tool.call(code="")
            assert isinstance(executed, ToolResult)
            assert isinstance(observed, ToolResult)
            assert len(observed.data.cells) == 1
            assert caprole.runtime_host.descriptor("jupyter:default").revision == 1
            await tool.call(close=True)

        run(scenario())

    def test_publish_failure_reports_committed_partial_success(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_publish_fail")
        tool.get_artifact_publisher = lambda: FailingArtifactPublisher()

        async def scenario():
            with pytest.raises(ToolError) as caught:
                await tool.call(code="committed_value = 11")
            error = caught.value
            assert error.context == {
                "partial_success": True,
                "committed_runtime": "jupyter:default",
                "committed_revision": 1,
                "failed_stage": "artifact_publish",
            }
            assert "Do not rerun the code" in str(error)
            descriptor = caprole.runtime_host.descriptor("jupyter:default")
            assert descriptor.revision == 1
            assert descriptor.state is RuntimeState.READY
            async with caprole.runtime_host.access("jupyter:default", mode="read", owner_id="test:partial") as access:
                driver = access.driver
                assert isinstance(driver, KernelRuntimeDriver)
                document = driver.snapshot_document()
            assert document.cells[-1].source == "committed_value = 11"
            await tool.call(close=True)

        run(scenario())

    def test_stdout_captured(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_out")

        async def scenario():
            result = await tool.call(code="print('hello')")
            assert isinstance(result, ToolResult)
            assert "hello" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_expression_repr(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_repr")

        async def scenario():
            result = await tool.call(code="40 + 2")
            assert isinstance(result, ToolResult)
            assert "42" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_state_persists_across_calls(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_state")

        async def scenario():
            await tool.call(code="x = 100")
            result = await tool.call(code="print(x + 1)")
            assert isinstance(result, ToolResult)
            assert "101" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_error_traceback_ansi_stripped(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_err")

        async def scenario():
            result = await tool.call(code="raise ValueError('boom')")
            assert isinstance(result, ToolResult)
            assert "ValueError" in result.output
            assert "boom" in result.output
            assert "\x1b[" not in result.output  # ANSI stripped
            await tool.call(close=True)

        run(scenario())

    def test_cwd_seeded_from_role(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_cwd")

        async def scenario():
            result = await tool.call(code="import os; print(os.getcwd())")
            assert isinstance(result, ToolResult)
            assert str(workspace) in result.output
            await tool.call(close=True)

        run(scenario())


# ---------------------------------------------------------------------------
# timeout -> interrupt + partial output, state preserved
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_interrupts_and_preserves_state(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_to")

        async def scenario():
            await tool.call(code="y = 7")
            result = await tool.call(
                code="import time\nprint('start', flush=True)\ntime.sleep(30)",
                timeout=2,
            )
            assert isinstance(result, ToolResult)
            assert "timed out" in result.output
            assert "start" in result.output
            # State survived the interrupt.
            alive = await tool.call(code="print(y)")
            assert isinstance(alive, ToolResult)
            assert "7" in alive.output
            await tool.call(close=True)

        run(scenario())


# ---------------------------------------------------------------------------
# interrupt / restart / close
# ---------------------------------------------------------------------------


class TestControl:
    def test_restart_clears_state(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_restart")

        async def scenario():
            await tool.call(code="z = 999")
            msg = await tool.call(restart=True)
            assert "restarted" in msg
            result = await tool.call(code="print('z' in dir())")
            assert isinstance(result, ToolResult)
            assert "False" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_interrupt_without_running_code(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_int")

        async def scenario():
            await tool.call(code="pass")
            msg = await tool.call(interrupt=True)
            assert "interrupted" in msg
            await tool.call(close=True)

        run(scenario())

    def test_interrupt_without_kernel_raises(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_noint")
        with pytest.raises(ToolError):
            run(tool.call(interrupt=True))

    def test_close_no_kernel(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_close0")
        out = run(tool.call(close=True))
        assert "no kernel to close" in out

    def test_close_after_use(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_close1")

        async def scenario():
            await tool.call(code="a = 1")
            out = await tool.call(close=True)
            assert "kernel closed" in out
            assert not _has_kernel(caprole)

        run(scenario())

    def test_cleanup_terminates_live_kernel(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_cleanup")

        async def scenario():
            await tool.call(code="b = 2")
            assert _has_kernel(caprole)
            await tool.cleanup_session("k_cleanup")
            assert not _has_kernel(caprole)
            await tool.cleanup_session("k_cleanup")  # idempotent

        run(scenario())


# ---------------------------------------------------------------------------
# helpers (unit)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_strip_ansi(self):
        assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_cap_text_keeps_head_tail(self):
        text = "H" * 10 + "M" * 2_000_000 + "T" * 10
        capped = cap_head_tail(text, OUTPUT_MAX_CHARS)[0]
        assert "omitted" in capped
        assert capped.startswith("H")
        assert capped.endswith("T")
        assert len(capped) < len(text)

    def test_cap_text_short_unchanged(self):
        assert cap_head_tail("short", OUTPUT_MAX_CHARS)[0] == "short"

    def test_iopub_accumulator_preserves_safe_notebook_outputs(self):
        parts = []
        outputs = []
        execution_count = [None]

        KernelSession._accumulate(
            {"msg_type": "execute_input", "content": {"execution_count": 7}},
            parts,
            outputs,
            execution_count,
        )
        KernelSession._accumulate(
            {
                "msg_type": "display_data",
                "content": {
                    "data": {
                        "text/plain": "<image>",
                        "image/png": "ZmFrZQ==",
                        "text/html": "<script>ignored()</script>",
                    }
                },
            },
            parts,
            outputs,
            execution_count,
        )

        assert execution_count == [7]
        assert outputs[0].data == {
            "text/plain": "<image>",
            "image/png": "ZmFrZQ==",
        }

    def test_iopub_accumulator_separates_display_updates(self):
        parts = []
        outputs = []
        display_updates = []

        KernelSession._accumulate(
            {
                "msg_type": "display_data",
                "content": {
                    "data": {"text/plain": "first"},
                    "transient": {"display_id": "shared"},
                },
            },
            parts,
            outputs,
            display_updates=display_updates,
        )
        KernelSession._accumulate(
            {
                "msg_type": "update_display_data",
                "content": {
                    "data": {"text/plain": "final"},
                    "transient": {"display_id": "shared"},
                },
            },
            parts,
            outputs,
            display_updates=display_updates,
        )

        assert outputs[0].display_id == "shared"
        assert outputs[0].data == {"text/plain": "first"}
        assert [(item.display_id, item.data) for item in display_updates] == [("shared", {"text/plain": "final"})]


# ---------------------------------------------------------------------------
# kernel-state capture / restore (session resume)
# ---------------------------------------------------------------------------


class TestStateCaptureRestore:
    def test_capture_records_cwd_and_env_diff(self, caprole, workspace):
        """After chdir + environ set, a call records (cwd, env_diff) on the Role."""
        sub = workspace / "sub"
        sub.mkdir()
        tool = bind(Python(), caprole, session_id="k_cap")

        async def scenario():
            await tool.call(code=("import os\n" f"os.chdir({str(sub)!r})\n" "os.environ['CAP_FOO'] = 'cap_bar'"))
            await tool.cleanup_session("k_cap")

        run(scenario())
        state = caprole.latest_runtime_state("jupyter", "jupyter-state+json@2")
        env = state["env"]
        assert state["cwd"] == str(sub)
        assert env.get("CAP_FOO") == "cap_bar"
        # Noise keys must be filtered out of the diff.
        assert "PWD" not in env and "SHLVL" not in env and "_" not in env

    def test_checkpoint_restores_notebook_document_without_reexecuting_cells(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_notebook_restore")

        async def scenario():
            executed = await tool.call(code="restored_value = 21 * 2")
            assert isinstance(executed, ToolResult)
            checkpoint = await caprole.runtime_host.checkpoint(
                "jupyter:default",
                reason="test-notebook-restore",
            )
            assert checkpoint.codec == "jupyter-state+json@2"
            await tool.call(close=True)
            caprole.runtime_host.stage_checkpoint(checkpoint)

            restored = await tool.call(code="")
            assert isinstance(restored, ToolResult)
            assert [cell.source for cell in restored.data.cells] == ["restored_value = 21 * 2"]
            assert restored.data.kernel_epoch == executed.data.kernel_epoch + 1
            assert caprole.runtime_host.descriptor("jupyter:default").revision == 1
            await tool.call(close=True)

        run(scenario())

    def test_restore_state_reseeds_new_kernel(self, caprole, workspace):
        """restore_state injects cwd/env into a fresh kernel (no user code rerun)."""
        sub = workspace / "restored"
        sub.mkdir()
        tool = bind(Python(), caprole, session_id="k_restore")

        async def scenario():
            await tool._ensure_runtime()
            async with caprole.runtime_host.access("jupyter:default", mode="write", owner_id="test:restore") as access:
                access.commit()
                driver = access.driver
                assert isinstance(driver, KernelRuntimeDriver)
                await driver.session.restore_state(str(sub), {"REZ_FOO": "rez_bar"}, [])
            result = await tool.call(code="import os; print(os.getcwd()); print(os.environ.get('REZ_FOO'))")
            assert isinstance(result, ToolResult)
            assert str(sub) in result.output
            assert "rez_bar" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_pending_restore_applied_on_ensure_session(self, caprole, workspace):
        """Runtime creation consumes the pending restore and re-seeds the kernel."""
        sub = workspace / "pending"
        sub.mkdir()
        caprole.stage_runtime_checkpoint(
            "jupyter",
            "jupyter-state+json@1",
            {"cwd": str(sub), "env": {"PEND_FOO": "pend_bar"}, "unset": []},
        )
        tool = bind(Python(), caprole, session_id="k_pending")

        async def scenario():
            await tool._ensure_runtime()  # applies pending restore once
            result = await tool.call(code="import os; print(os.getcwd()); print(os.environ.get('PEND_FOO'))")
            assert isinstance(result, ToolResult)
            assert str(sub) in result.output
            assert "pend_bar" in result.output
            await tool.call(close=True)

        run(scenario())

    def test_restore_value_is_reprd_no_injection(self, caprole, workspace):
        """A value with code metacharacters is taken literally (no eval)."""
        tool = bind(Python(), caprole, session_id="k_quote")

        async def scenario():
            await tool._ensure_runtime()
            # repr() embeds the value as a literal — the embedded expression is
            # never evaluated.
            async with caprole.runtime_host.access("jupyter:default", mode="write", owner_id="test:quote") as access:
                access.commit()
                driver = access.driver
                assert isinstance(driver, KernelRuntimeDriver)
                await driver.session.restore_state("", {"INJ": "__import__('os').getcwd()"}, [])
            result = await tool.call(code="import os; print(repr(os.environ.get('INJ')))")
            assert isinstance(result, ToolResult)
            assert "__import__('os').getcwd()" in result.output
            await tool.call(close=True)

        run(scenario())


class TestLiveSurface:
    def test_finish_handoff_interrupts_running_human_cell(self, caprole):
        tool = bind(Python(), caprole, session_id="k_finish_running")

        async def scenario():
            await tool.call(code="pass")
            async with caprole.runtime_host.access(
                "jupyter:default", mode="read", owner_id="test:finish-running"
            ) as access:
                driver = access.driver
                assert isinstance(driver, KernelRuntimeDriver)
            handle = await driver.prepare_handoff(
                HandoffRequest(runtime_ref=RuntimeRef(runtime_id="j-running", kind="jupyter"))
            )
            execution = asyncio.create_task(
                driver.send_surface_input(
                    handle,
                    SurfaceInput(
                        kind="notebook.execute",
                        data=json.dumps(
                            {
                                "cell_id": "cell-running",
                                "source": "import time; time.sleep(30)",
                            }
                        ),
                    ),
                )
            )
            for _ in range(100):
                if driver.snapshot_document().kernel_status == "busy":
                    break
                await asyncio.sleep(0.02)
            await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.CANCELLED))
            await asyncio.wait_for(execution, timeout=10)
            assert driver.snapshot_document().kernel_status == "idle"
            await driver.detach_surface(handle)
            await tool.call(close=True)

        run(scenario())

    def test_display_id_update_replaces_all_existing_outputs(self, caprole):
        tool = bind(Python(), caprole, session_id="k_display_id")

        async def scenario():
            await tool.call(code="from IPython.display import display\ndisplay('first', display_id='shared')")
            await tool.call(code="display('second', display_id='shared')")
            await tool.call(
                code="from IPython.display import update_display\nupdate_display('final', display_id='shared')"
            )
            document = caprole.runtime_host.descriptor("jupyter:default")
            assert document.revision == 3
            async with caprole.runtime_host.access(
                "jupyter:default", mode="read", owner_id="test:display-id"
            ) as access:
                driver = access.driver
                assert isinstance(driver, KernelRuntimeDriver)
                snapshot = driver.snapshot_document()
            linked = [output for cell in snapshot.cells for output in cell.outputs if output.display_id == "shared"]
            assert len(linked) == 2
            assert all(output.data["text/plain"] == "'final'" for output in linked)
            assert snapshot.cells[-1].outputs == []
            await tool.call(close=True)

        run(scenario())

    def test_handoff_fences_and_replies_to_kernel_stdin(self, caprole):
        tool = bind(Python(), caprole, session_id="k_stdin")

        async def scenario():
            await tool.call(code="seed = 1")
            async with caprole.runtime_host.access("jupyter:default", mode="read", owner_id="test:stdin") as access:
                driver = access.driver
                assert isinstance(driver, KernelRuntimeDriver)
            request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="j-stdin", kind="jupyter"))
            handle = await driver.prepare_handoff(request)
            execution = asyncio.create_task(
                driver.send_surface_input(
                    handle,
                    SurfaceInput(
                        kind="notebook.execute",
                        data=json.dumps(
                            {
                                "cell_id": "cell-stdin",
                                "source": "name = input('Name: '); print('hello', name)",
                            }
                        ),
                    ),
                )
            )
            pending = None
            for _ in range(100):
                pending = driver.snapshot_document().input_request
                if pending is not None:
                    break
                await asyncio.sleep(0.02)
            assert pending is not None
            assert pending.cell_id == "cell-stdin"
            assert pending.prompt == "Name: "
            await driver.send_surface_input(
                handle,
                SurfaceInput(
                    kind="notebook.input_reply",
                    data=json.dumps(
                        {
                            "request_id": pending.request_id,
                            "value": "Ada",
                            "document_revision": pending.document_revision,
                            "kernel_epoch": pending.kernel_epoch,
                            "connection_generation": pending.connection_generation,
                            "human_generation": pending.human_generation,
                            "expected_request_revision": pending.request_revision,
                        }
                    ),
                ),
            )
            await asyncio.wait_for(execution, timeout=10)
            snapshot = driver.snapshot_document()
            assert snapshot.input_request is None
            assert snapshot.cells[-1].outputs[-1].text == "hello Ada\n"
            await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))
            with pytest.raises(RuntimeError, match="not current"):
                await driver.send_surface_input(
                    handle,
                    SurfaceInput(
                        kind="notebook.input_reply",
                        data=json.dumps(
                            {
                                "request_id": pending.request_id,
                                "value": "late",
                                "document_revision": pending.document_revision,
                                "kernel_epoch": pending.kernel_epoch,
                                "connection_generation": pending.connection_generation,
                                "human_generation": pending.human_generation,
                                "expected_request_revision": pending.request_revision,
                            }
                        ),
                    ),
                )
            await driver.detach_surface(handle)
            await tool.call(close=True)

        run(scenario())

    def test_handoff_notebook_executes_code_and_keeps_observing(self, caprole):
        tool = bind(Python(), caprole, session_id="k_surface")

        async def scenario():
            await tool.call(code="seed = 40")
            async with caprole.runtime_host.access("jupyter:default", mode="read", owner_id="test:surface") as access:
                driver = access.driver
                assert isinstance(driver, KernelRuntimeDriver)
            request = HandoffRequest(runtime_ref=RuntimeRef(runtime_id="j-1", kind="jupyter"))
            handle = await driver.prepare_handoff(request)
            assert handle.surface.kind == "notebook"
            assert handle.surface.presentation is SurfacePresentationMode.WINDOW
            await driver.send_surface_input(
                handle,
                SurfaceInput(
                    kind="notebook.execute",
                    data=json.dumps({"cell_id": "cell-human", "source": "print(seed + 2)"}),
                ),
            )
            frame = await driver.snapshot_surface(handle)
            assert frame.media_type == NOTEBOOK_MEDIA_TYPE
            document = NotebookDocument.model_validate_json(frame.content)
            assert document.cells[-1].source == "print(seed + 2)"
            assert document.cells[-1].origin == "human"
            assert document.cells[-1].outputs[0].text == "42\n"
            await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))
            with pytest.raises(RuntimeError, match="not current"):
                await driver.send_surface_input(
                    handle,
                    SurfaceInput(
                        kind="notebook.execute",
                        data=json.dumps({"cell_id": "cell-late", "source": "print('late')"}),
                    ),
                )
            await driver.execute("print('agent update')", 30)
            observed = NotebookDocument.model_validate_json((await driver.snapshot_surface(handle)).content)
            assert observed.cells[-1].origin == "agent"
            assert observed.cells[-1].outputs[0].text == "agent update\n"
            await driver.detach_surface(handle)
            await tool.call(close=True)

        run(scenario())
