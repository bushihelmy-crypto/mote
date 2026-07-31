"""Apply semantic output-compression to a settled ToolResult.

Bridges the generic :func:`compress_output` engine to a concrete tool call: it
resolves the shell command a tool ran, compresses the recognised output,
persists the *full* original to disk (a ``raw-`` id namespace, distinct from the
size-cap's own file), and prepends a marker naming that file so the model can
``Read`` the exact original on demand.

Fail-safe: media results, empty/already-persisted output, the config-off case,
and unrecognised commands all return the result untouched; ``result.success`` is
never modified so a failed command's exit signal is preserved.
"""

from __future__ import annotations

import uuid

from mote.contracts.config.tool import PERSISTED_OUTPUT_OPEN_TAG, ToolResultLimitConfig
from mote.runtime.resources import spill as tool_result_limit
from mote.runtime.telemetry.logging import logger
from mote.runtime.tools.compress import compress_output
from mote.runtime.tools.tool_result import ToolResult


def compress_tool_result(
    result: ToolResult,
    name: str,
    args: dict,
    *,
    session_id: str,
    config: ToolResultLimitConfig,
) -> ToolResult:
    """Structurally compress a shell tool's output when it is understood.

    Applied only for the shell tools (Bash/Terminal, plus a Jupyter ``!shell``
    magic), where the LLM-issued command is known, and only when
    :func:`compress_output` recognises the command family and produces something
    smaller. Runs BEFORE the size cap so the cap bounds whatever remains.
    """
    if not config.enable_output_compression or not result.output:
        return result
    # Media goes to the model verbatim; never rewrite it.
    if result.media:
        return result
    # Already wrapped by the size-cap layer on a prior turn — leave it.
    if result.output.startswith(PERSISTED_OUTPUT_OPEN_TAG):
        return result

    command = _command_for_compression(name, args)
    if not command:
        return result

    outcome = compress_output(
        command,
        result.output,
        min_chars=config.compression_min_output_chars,
        max_input_chars=config.compression_max_input_chars,
    )
    if not outcome.applied:
        return result

    # Persist the full original first, so the marker can name its path. The
    # ``raw-`` id namespace keeps it distinct from the size-cap's own file.
    raw_id = f"raw-{uuid.uuid4().hex}"
    full_path = tool_result_limit.persist_result(result.output, raw_id, session_id, None)
    location = f"; full output: {full_path}" if full_path else ""
    marker = f"[compressed: {outcome.label}; saved {outcome.saved_chars} chars{location}]"
    logger.debug(
        f"compress_tool_result: compressed {name} output via {outcome.label} "
        f"({outcome.original_chars} -> {outcome.compressed_chars} chars)"
    )
    result.output = f"{marker}\n{outcome.text}"
    return result


def _command_for_compression(name: str, args: dict) -> str | None:
    """Best-effort command line for routing a shell tool's output.

    Bash carries the exact command in ``args["command"]``. Terminal drives a
    persistent PTY; its ``args["input"]`` may be interactive keystrokes, so only
    the first line is used as a routing hint (``command_prefix`` is tolerant, and
    an unrecognised prefix simply skips compression).

    Jupyter runs *Python code*, not shell commands, so it is only routed for an
    IPython ``!shell`` magic on the first line (``!pytest`` / ``!git diff``): the
    leading ``!`` is stripped and the rest treated as a command. A plain-Python
    first line yields ``None`` — never sniffed as a command, so ordinary
    ``print()`` output is never mistaken for pytest/lint output.

    Any other tool returns ``None`` (not compressed).
    """
    if name == "Bash":
        command = args.get("command")
        return command if isinstance(command, str) else None
    if name == "Terminal":
        value = args.get("input")
        if isinstance(value, str) and value.strip():
            return value.splitlines()[0]
        return None
    if name in ("Jupyter", "Python"):
        code = args.get("code")
        if not isinstance(code, str):
            return None
        first = code.splitlines()[0].strip() if code.splitlines() else ""
        # Only an IPython ``!shell`` magic is a real command; strip the ``!``.
        if first.startswith("!"):
            return first[1:].strip() or None
        return None
    return None
