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

from mote.contracts.hook import HookStop
from mote.runtime.hook.types import EMPTY, HookOutcome
from mote.runtime.telemetry.logging import logger

_HOOK_OUTPUT_FIELDS = frozenset(
    {
        "decision",
        "hookSpecificOutput",
        "permissionDecisionReason",
        "reason",
        "updatedInput",
        "updatedResponse",
        "additionalContext",
        "systemMessage",
        "continue",
        "stopReason",
    }
)
_HOOK_SPECIFIC_FIELDS = frozenset({"permissionDecision", "additionalContext"})


def _validate_context(value: object, *, path: str) -> None:
    if value is None or type(value) is str:
        return
    if type(value) is list and all(type(item) is str for item in value):
        return
    raise ValueError(f"{path} must be a string or list of strings")


def _validate_strict_output(obj: dict[object, object]) -> None:
    if any(type(key) is not str for key in obj) or not set(obj).issubset(_HOOK_OUTPUT_FIELDS):
        raise ValueError("hook output contains unknown fields")
    decision = obj.get("decision")
    if decision is not None and decision not in ("approve", "block"):
        raise ValueError("unknown hook decision")
    for field in ("permissionDecisionReason", "reason", "updatedResponse", "systemMessage", "stopReason"):
        value = obj.get(field)
        if value is not None and type(value) is not str:
            raise ValueError(f"hook output {field} must be a string")
    continuation = obj.get("continue")
    if continuation is not None and type(continuation) is not bool:
        raise ValueError("hook output continue must be a boolean")
    updated_input = obj.get("updatedInput")
    if updated_input is not None and type(updated_input) is not dict:
        raise ValueError("hook output updatedInput must be an object")
    _validate_context(obj.get("additionalContext"), path="hook output additionalContext")
    specific = obj.get("hookSpecificOutput")
    if specific is None:
        return
    if type(specific) is not dict or any(type(key) is not str for key in specific):
        raise ValueError("hookSpecificOutput must be an object")
    if not set(specific).issubset(_HOOK_SPECIFIC_FIELDS):
        raise ValueError("hookSpecificOutput contains unknown fields")
    permission = specific.get("permissionDecision")
    if permission is not None and permission not in ("allow", "deny", "ask"):
        raise ValueError("unknown hook permission decision")
    _validate_context(specific.get("additionalContext"), path="hookSpecificOutput additionalContext")


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
    behavior = None
    additional_context: list[str] = []

    # Coarse decision (least specific).
    decision = obj.get("decision")
    if decision == "approve":
        behavior = "allow"
    elif decision == "block":
        behavior = "deny"

    # hookSpecificOutput.permissionDecision is more specific and wins.
    hook_specific = obj.get("hookSpecificOutput")
    if isinstance(hook_specific, dict):
        perm = hook_specific.get("permissionDecision")
        if perm in ("allow", "deny", "ask"):
            behavior = perm
        ctx = hook_specific.get("additionalContext")
        additional_context.extend(_coerce_context(ctx))

    # Reason text -> system_message (only when not already supplied).
    reason = obj.get("permissionDecisionReason") or obj.get("reason")

    # updatedInput -> updated_args
    updated = obj.get("updatedInput")
    if isinstance(updated, dict):
        updated_args = updated
    else:
        updated_args = None

    updated_response = obj.get("updatedResponse")
    if isinstance(updated_response, str):
        resolved_response = updated_response
    else:
        resolved_response = None

    # additionalContext at top level too.
    additional_context.extend(_coerce_context(obj.get("additionalContext")))

    # systemMessage
    system_message = obj.get("systemMessage")
    if system_message:
        resolved_message = str(system_message)
    elif reason and behavior == "deny":
        # Surface a block reason even when no explicit systemMessage was given.
        resolved_message = str(reason)
    else:
        resolved_message = ""

    # continue: false -> stop
    if obj.get("continue") is False:
        stop = HookStop(str(obj.get("stopReason", "") or reason or ""))
    else:
        stop = None

    return HookOutcome(
        behavior=behavior,
        updated_args=updated_args,
        updated_response=resolved_response,
        additional_context=tuple(additional_context),
        system_message=resolved_message,
        stop=stop,
    )


def parse_command_output(stdout: str, stderr: str, exit_code: int, *, strict: bool = False) -> HookOutcome:
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
        if strict:
            raise ValueError("hook output must be a JSON object")
        return HookOutcome()  # non-JSON stdout -> no influence
    if not isinstance(obj, dict):
        if strict:
            raise ValueError("hook output must be a JSON object")
        return HookOutcome()
    if strict:
        _validate_strict_output(obj)
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
