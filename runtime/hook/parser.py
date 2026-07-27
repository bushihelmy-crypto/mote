"""Hook output parsing — the stdout + exit-code contract.

Two entry points turn a handler's result into a neutral :class:`HookOutcome`:

  * :func:`parse_command_output` — for external command handlers (the JSON
    stdin/stdout contract):
      - exit ``2``            => blocking (``behavior="deny"``, reason from stderr)
      - exit ``0`` + non-JSON => passthrough (no influence)
      - other nonzero        => non-blocking warning (logged), passthrough
      - JSON stdout          => mapped per the field table below
  * :func:`parse_callback_result` — for in-process Python callbacks: accept
    ``None`` (passthrough), a dict (same keys as the JSON contract), or a
    :class:`HookOutcome` returned directly.

JSON field mapping:
  * ``decision``: ``approve`` -> allow, ``block`` -> deny
  * ``hookSpecificOutput.permissionDecision``: ``allow``/``deny``/``ask`` (more
    specific; overrides ``decision``)
  * ``permissionDecisionReason`` / ``reason``: carried into ``system_message``
  * ``updatedInput`` -> ``updated_args``
  * ``updatedResponse`` -> ``updated_response``
  * ``additionalContext`` -> ``additional_context`` (str or list[str])
  * ``systemMessage`` -> ``system_message``
  * ``continue: false`` -> ``stop`` (+ ``stopReason``)
"""

from __future__ import annotations

import json
from typing import Any

from mote.runtime.hook.types import EMPTY, HookOutcome
from mote.runtime.logging import logger


def _coerce_context(value: Any) -> list[str]:
    """Normalize ``additionalContext`` into a list[str] (str -> [str])."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


def _outcome_from_obj(obj: dict) -> HookOutcome:
    """Map a decoded JSON/dict object onto a HookOutcome (field contract)."""
    outcome = HookOutcome()

    # Coarse decision (least specific).
    decision = obj.get("decision")
    if decision == "approve":
        outcome.behavior = "allow"
    elif decision == "block":
        outcome.behavior = "deny"

    # hookSpecificOutput.permissionDecision is more specific and wins.
    hook_specific = obj.get("hookSpecificOutput")
    if isinstance(hook_specific, dict):
        perm = hook_specific.get("permissionDecision")
        if perm in ("allow", "deny", "ask"):
            outcome.behavior = perm
        ctx = hook_specific.get("additionalContext")
        outcome.additional_context.extend(_coerce_context(ctx))

    # Reason text -> system_message (only when not already supplied).
    reason = obj.get("permissionDecisionReason") or obj.get("reason")

    # updatedInput -> updated_args
    updated = obj.get("updatedInput")
    if isinstance(updated, dict):
        outcome.updated_args = updated

    updated_response = obj.get("updatedResponse")
    if isinstance(updated_response, str):
        outcome.updated_response = updated_response

    # additionalContext at top level too.
    outcome.additional_context.extend(_coerce_context(obj.get("additionalContext")))

    # systemMessage
    system_message = obj.get("systemMessage")
    if system_message:
        outcome.system_message = str(system_message)
    elif reason and outcome.behavior == "deny":
        # Surface a block reason even when no explicit systemMessage was given.
        outcome.system_message = str(reason)

    # continue: false -> stop
    if obj.get("continue") is False:
        outcome.stop = True
        outcome.stop_reason = str(obj.get("stopReason", "") or reason or "")

    return outcome


def parse_command_output(stdout: str, stderr: str, exit_code: int) -> HookOutcome:
    """Turn a command handler's (stdout, stderr, exit_code) into a HookOutcome."""
    # Exit 2 is the "blocking" signal: deny, reason from stderr.
    if exit_code == 2:
        reason = (stderr or "").strip()
        return HookOutcome(behavior="deny", system_message=reason)

    if exit_code != 0:
        # Any other nonzero exit is a non-blocking failure: log + passthrough.
        msg = (stderr or stdout or "").strip()
        logger.warning(f"hook: command handler exited {exit_code} (non-blocking): {msg}")
        return HookOutcome()

    # Exit 0: structured JSON on stdout influences the host; anything else is a
    # plain passthrough.
    text = (stdout or "").strip()
    if not text:
        return EMPTY
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return HookOutcome()  # non-JSON stdout -> no influence
    if not isinstance(obj, dict):
        return HookOutcome()
    return _outcome_from_obj(obj)


def parse_callback_result(value: Any) -> HookOutcome:
    """Normalize a Python callback's return into a HookOutcome.

    Accepts ``None`` (passthrough), a :class:`HookOutcome` (returned as-is), or a
    dict using the same keys as the JSON contract.
    """
    if value is None:
        return EMPTY
    if isinstance(value, HookOutcome):
        return value
    if isinstance(value, dict):
        return _outcome_from_obj(value)
    # Unknown return type -> ignore (passthrough), but flag it.
    logger.warning(f"hook: callback returned unsupported type {type(value).__name__}; ignoring")
    return EMPTY


__all__ = ["parse_command_output", "parse_callback_result"]
