"""Domain policy for authorizing one tool call before invocation.

The pipeline has a fixed semantic order: the optional PreToolUse hook may
rewrite or narrow the intent, then the core permission engine evaluates the
final arguments.  Hook failure is advisory/fail-open; permission failure is
security-critical/fail-closed. Neither path uses loss-tolerant telemetry.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

from mote.contracts.authorization import PermissionDecision, PermissionFacts
from mote.contracts.config.tool import LoopGuardConfig
from mote.contracts.ports.tool.policy import (
    PermissionFactsResolver,
    ToolCallPolicyExtension,
    ToolCallPolicyExtensionSpec,
)
from mote.contracts.tool.policy import (
    ToolCallDecision,
    ToolCallIntent,
    ToolPolicyTraceEntry,
    ToolResultIntent,
    ToolResultPresentation,
)
from mote.runtime.content_hashing import content_hash
from mote.runtime.hook.manager import HookManager
from mote.runtime.secrets.policy import redact
from mote.runtime.secrets.store import SecretStore
from mote.runtime.tools.base_tool import ToolCapabilityProvider
from mote.runtime.tools.loop_guard.detector import ThrashDetector, Verdict
from mote.runtime.tools.permission.config import PermissionConfig
from mote.runtime.tools.permission.engine import PermissionEngine
from mote.runtime.tools.permission.rule_store import RuleStore
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_TOOL_POLICY_TIMEOUT = 120.0
_REDACTION_FAILURE_OUTPUT = "[tool output withheld because the secret-redaction policy was unavailable]"


def _tool_call_signature(tool_name: str, arguments: dict) -> str:
    return json.dumps(
        [{"name": tool_name, "args": arguments}],
        sort_keys=True,
        ensure_ascii=False,
    )


@dataclass(frozen=True)
class _InstalledExtension:
    spec: ToolCallPolicyExtensionSpec
    extension: ToolCallPolicyExtension


class DefaultToolCallPolicy:
    """Sealed Hook rewrite → Permission gate policy for tool calls."""

    def __init__(
        self,
        *,
        hook_manager: Optional[HookManager] = None,
        permission_engine: Optional[PermissionEngine] = None,
        permission_mode: Optional[str] = None,
        extensions: tuple[ToolCallPolicyExtensionSpec, ...] = (),
        timeout: float = DEFAULT_TOOL_POLICY_TIMEOUT,
    ) -> None:
        self._hook_manager = hook_manager
        self._permission_engine = permission_engine
        self._permission_mode = permission_mode
        self._manifest, self._extensions = self._install_extensions(extensions)
        self._timeout = timeout

    @staticmethod
    def _install_extensions(
        extensions: tuple[ToolCallPolicyExtensionSpec, ...],
    ) -> tuple[
        tuple[ToolCallPolicyExtensionSpec, ...],
        tuple[_InstalledExtension, ...],
    ]:
        sealed = tuple(extensions)
        identities: set[str] = set()
        installed: list[_InstalledExtension] = []
        for spec in sealed:
            if not spec.identity or not spec.identity.strip():
                raise ValueError("tool policy extension identity must not be empty")
            if spec.identity in identities:
                raise ValueError(f"duplicate tool policy extension identity: {spec.identity}")
            if spec.timeout <= 0:
                raise ValueError(f"tool policy extension timeout must be positive: {spec.identity}")
            identities.add(spec.identity)
            extension = spec.factory()
            if not callable(getattr(extension, "inspect", None)):
                raise TypeError("tool policy extension factory must return an inspector: " f"{spec.identity}")
            installed.append(_InstalledExtension(spec, extension))
        return sealed, tuple(installed)

    @property
    def manifest(self) -> tuple[ToolCallPolicyExtensionSpec, ...]:
        """The sealed, deterministic extension roster."""
        return self._manifest

    async def authorize(
        self,
        intent: ToolCallIntent,
        resolve_permission_facts: PermissionFactsResolver,
    ) -> ToolCallDecision:
        arguments = dict(intent.arguments)
        trace: list[ToolPolicyTraceEntry] = []

        hook_decision = await self._apply_hook(intent, arguments, trace)
        if hook_decision is not None:
            return hook_decision

        extension_decision = await self._apply_extensions(
            intent,
            arguments,
            resolve_permission_facts,
            trace,
        )
        if extension_decision is not None:
            return extension_decision

        permission_decision = await self._apply_permission(
            intent,
            arguments,
            resolve_permission_facts,
            trace,
        )
        if permission_decision is not None:
            return permission_decision

        trace.append(ToolPolicyTraceEntry(step="final_authorization", disposition="allow"))
        return ToolCallDecision.allow(intent.identity.with_arguments(arguments), arguments, trace=tuple(trace))

    async def _apply_hook(
        self,
        intent: ToolCallIntent,
        arguments: dict,
        trace: list[ToolPolicyTraceEntry],
    ) -> Optional[ToolCallDecision]:
        manager = self._hook_manager
        if manager is None:
            return None
        try:
            outcome = await asyncio.wait_for(
                manager.fire(
                    "PreToolUse",
                    {
                        "tool_name": intent.tool_name,
                        "tool_input": arguments,
                        "identity": intent.identity,
                    },
                    permission_mode=self._permission_mode,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            trace.append(
                ToolPolicyTraceEntry(
                    step="pre_tool_use_hook",
                    disposition="deny",
                    detail="timeout",
                )
            )
            return ToolCallDecision.deny(
                intent.identity.with_arguments(arguments),
                arguments,
                "control hook timed out",
                trace=tuple(trace),
            )
        except Exception as exc:  # noqa: BLE001 -- control hooks fail closed
            trace.append(
                ToolPolicyTraceEntry(
                    step="pre_tool_use_hook",
                    disposition="deny",
                    detail=type(exc).__name__,
                )
            )
            return ToolCallDecision.deny(
                intent.identity.with_arguments(arguments),
                arguments,
                "control hook failed",
                trace=tuple(trace),
            )

        for fact in outcome.authorization_facts:
            trace.append(
                ToolPolicyTraceEntry(
                    step=f"hook_command:{fact.handler_id}",
                    disposition=fact.disposition,
                )
            )

        if outcome.updated_args is not None:
            if not isinstance(outcome.updated_args, Mapping):
                trace.append(
                    ToolPolicyTraceEntry(
                        step="pre_tool_use_hook",
                        disposition="deny",
                        detail="invalid argument rewrite",
                    )
                )
                return ToolCallDecision.deny(
                    intent.identity.with_arguments(arguments),
                    arguments,
                    "control hook returned an invalid argument rewrite",
                    trace=tuple(trace),
                )
            else:
                rewritten_fields = tuple(sorted(set(arguments) | set(outcome.updated_args)))
                arguments.clear()
                arguments.update(dict(outcome.updated_args))
                trace.append(
                    ToolPolicyTraceEntry(
                        step="pre_tool_use_hook",
                        disposition="rewrite",
                        rewritten_fields=rewritten_fields,
                    )
                )

        if outcome.behavior == "deny" or outcome.stop is not None:
            reason = (
                outcome.system_message
                or (outcome.stop.reason if outcome.stop is not None else "")
                or "blocked before tool use"
            )
            trace.append(
                ToolPolicyTraceEntry(
                    step="pre_tool_use_hook",
                    disposition="stop" if outcome.stop is not None else "deny",
                    detail="hook denied call",
                )
            )
            return ToolCallDecision.deny(
                intent.identity.with_arguments(arguments),
                arguments,
                reason,
                terminate=outcome.stop is not None,
                trace=tuple(trace),
            )

        if outcome.behavior is not None and outcome.updated_args is None:
            trace.append(
                ToolPolicyTraceEntry(
                    step="pre_tool_use_hook",
                    disposition="allow",
                    detail=outcome.behavior,
                )
            )
        return None

    async def _apply_extensions(
        self,
        intent: ToolCallIntent,
        arguments: dict,
        resolve_permission_facts: PermissionFactsResolver,
        trace: list[ToolPolicyTraceEntry],
    ) -> Optional[ToolCallDecision]:
        if not self._extensions:
            return None
        current_intent = replace(intent, arguments=dict(arguments))
        for installed in self._extensions:
            spec = installed.spec
            step = f"extension:{spec.identity}"
            try:
                facts = resolve_permission_facts(arguments)
                verdict = await asyncio.wait_for(
                    installed.extension.inspect(current_intent, facts),
                    timeout=min(spec.timeout, self._timeout),
                )
                if not isinstance(verdict.allowed, bool):
                    raise TypeError("extension verdict 'allowed' must be bool")
            except asyncio.TimeoutError:
                reason = f"tool policy extension '{spec.identity}' timed out; " "denied for safety."
                trace.append(
                    ToolPolicyTraceEntry(
                        step=step,
                        disposition="failed_closed",
                        detail="timeout",
                    )
                )
                return ToolCallDecision.deny(
                    intent.identity.with_arguments(arguments),
                    arguments,
                    reason,
                    trace=tuple(trace),
                )
            except Exception as exc:  # noqa: BLE001 -- deny-only gate fails closed
                reason = f"tool policy extension '{spec.identity}' failed; " "denied for safety."
                trace.append(
                    ToolPolicyTraceEntry(
                        step=step,
                        disposition="failed_closed",
                        detail=type(exc).__name__,
                    )
                )
                return ToolCallDecision.deny(
                    intent.identity.with_arguments(arguments),
                    arguments,
                    reason,
                    trace=tuple(trace),
                )
            if not verdict.allowed:
                reason = verdict.reason or (f"tool policy extension '{spec.identity}' denied the call")
                trace.append(
                    ToolPolicyTraceEntry(
                        step=step,
                        disposition="deny",
                        detail="extension denied call",
                    )
                )
                return ToolCallDecision.deny(
                    intent.identity.with_arguments(arguments),
                    arguments,
                    reason,
                    trace=tuple(trace),
                )
            trace.append(ToolPolicyTraceEntry(step=step, disposition="allow"))
        return None

    async def _apply_permission(
        self,
        intent: ToolCallIntent,
        arguments: dict,
        resolve_permission_facts: PermissionFactsResolver,
        trace: list[ToolPolicyTraceEntry],
    ) -> Optional[ToolCallDecision]:
        engine = self._permission_engine
        if engine is None:
            return None
        try:
            async with asyncio.timeout(self._timeout):
                facts = resolve_permission_facts(arguments)
                decision = await self._check_permission(
                    engine,
                    intent.tool_name,
                    facts,
                )
                self._validate_permission_decision(decision)
                if decision.updated_args is not None:
                    arguments.clear()
                    arguments.update(decision.updated_args)
                    trace.append(
                        ToolPolicyTraceEntry(
                            step="permission",
                            disposition="rewrite",
                            rewritten_fields=tuple(sorted(arguments)),
                        )
                    )
                    final_facts = resolve_permission_facts(arguments)
                    final_decision = await self._check_permission(
                        engine,
                        intent.tool_name,
                        final_facts,
                    )
                    self._validate_permission_decision(final_decision)
                    if final_decision.updated_args is not None and final_decision.updated_args != arguments:
                        raise RuntimeError("permission argument rewrite did not converge")
                    decision = final_decision
        except asyncio.TimeoutError:
            reason = "permission policy could not evaluate the request (timeout); denied for safety."
            trace.append(
                ToolPolicyTraceEntry(
                    step="permission",
                    disposition="failed_closed",
                    detail="timeout",
                )
            )
            return ToolCallDecision.deny(
                intent.identity.with_arguments(arguments), arguments, reason, trace=tuple(trace)
            )
        except Exception as exc:  # noqa: BLE001 -- security gate fails closed
            reason = "permission policy could not evaluate the request (error); denied for safety."
            trace.append(
                ToolPolicyTraceEntry(
                    step="permission",
                    disposition="failed_closed",
                    detail=type(exc).__name__,
                )
            )
            return ToolCallDecision.deny(
                intent.identity.with_arguments(arguments), arguments, reason, trace=tuple(trace)
            )

        if decision.behavior == "ask":
            trace.append(
                ToolPolicyTraceEntry(
                    step="permission",
                    disposition="enrich",
                    detail=decision.reason.type,
                )
            )
            return ToolCallDecision.require_approval(
                intent.identity.with_arguments(arguments),
                arguments,
                reason=decision.message or decision.reason.detail,
                permission_targets=facts.targets,
                mutates_fs=facts.mutates_fs,
                trace=tuple(trace),
            )
        if decision.behavior == "deny":
            reason = decision.message or decision.reason.detail or "blocked before tool use"
            terminate = decision.reason.type == "user"
            trace.append(
                ToolPolicyTraceEntry(
                    step="permission",
                    disposition="stop" if terminate else "deny",
                    detail=decision.reason.type,
                )
            )
            return ToolCallDecision.deny(
                intent.identity.with_arguments(arguments),
                arguments,
                reason,
                terminate=terminate,
                trace=tuple(trace),
            )
        trace.append(ToolPolicyTraceEntry(step="permission", disposition="allow"))
        return None

    @staticmethod
    def _validate_permission_decision(decision: PermissionDecision) -> None:
        if decision.behavior not in ("allow", "deny", "ask"):
            raise ValueError(f"permission engine returned unknown behavior: {decision.behavior}")
        if decision.updated_args is not None and not isinstance(decision.updated_args, dict):
            raise TypeError("permission updated_args must be a dict")

    @staticmethod
    async def _check_permission(
        engine: PermissionEngine,
        tool_name: str,
        facts: PermissionFacts,
    ) -> PermissionDecision:
        if len(facts.targets) > 1:
            return await engine.check_multi(
                tool_name,
                targets=facts.targets,
                tool_check=facts.tool_check,
                mutates_fs=facts.mutates_fs,
            )
        return await engine.check(
            tool_name,
            target=facts.targets[0] if facts.targets else "",
            tool_check=facts.tool_check,
            mutates_fs=facts.mutates_fs,
            segments=facts.segments,
        )


def _render_loop_nudge(verdict: Verdict) -> str:
    if verdict.kind == "repeated_failure":
        what = (
            f"[loop guard] '{verdict.tool_name}' has now failed "
            f"{verdict.count} times in a row with the same arguments."
        )
    else:
        what = (
            f"[loop guard] '{verdict.tool_name}' has returned the same result "
            f"{verdict.count} times in a row — this read is not making progress."
        )
    return (
        f"{what} Stop reissuing it unchanged: change your approach, or if you "
        "are blocked, use AskUserQuestion to ask the user for guidance."
    )


class DefaultToolResultPolicy:
    """Safe representation policy that cannot rewrite execution settlement."""

    def __init__(
        self,
        *,
        hook_manager: Optional[HookManager] = None,
        secret_store: Optional[SecretStore] = None,
        loop_detector: Optional[ThrashDetector] = None,
        timeout: float = DEFAULT_TOOL_POLICY_TIMEOUT,
    ) -> None:
        self._hook_manager = hook_manager
        self._secret_store = secret_store
        self._loop_detector = loop_detector
        self._timeout = timeout

    async def present(self, intent: ToolResultIntent) -> ToolResultPresentation:
        output = intent.output
        terminate = False
        trace: list[ToolPolicyTraceEntry] = []

        output = self._redact(output, trace, step="secret_redaction")
        if intent.executed:
            output, terminate = await self._apply_hook(
                intent,
                output,
                trace,
            )
            output = self._apply_loop_guard(intent, output, trace)
        output = self._redact(output, trace, step="final_secret_redaction")
        trace.append(
            ToolPolicyTraceEntry(
                step="final_representation",
                disposition="allow",
            )
        )
        return ToolResultPresentation(
            output=output,
            terminate=terminate,
            trace=tuple(trace),
        )

    def _redact(
        self,
        output: str,
        trace: list[ToolPolicyTraceEntry],
        *,
        step: str,
    ) -> str:
        store = self._secret_store
        if store is None or not output:
            return output
        try:
            redacted, hits = redact(output, store.as_map())
        except Exception as exc:  # noqa: BLE001 -- disclosure boundary fails closed
            trace.append(
                ToolPolicyTraceEntry(
                    step=step,
                    disposition="failed_closed",
                    detail=type(exc).__name__,
                )
            )
            return _REDACTION_FAILURE_OUTPUT
        if hits:
            trace.append(ToolPolicyTraceEntry(step=step, disposition="redact"))
        return redacted

    async def _apply_hook(
        self,
        intent: ToolResultIntent,
        safe_output: str,
        trace: list[ToolPolicyTraceEntry],
    ) -> tuple[str, bool]:
        manager = self._hook_manager
        if manager is None:
            return safe_output, False
        try:
            outcome = await asyncio.wait_for(
                manager.fire(
                    "PostToolUse",
                    {
                        "tool_name": intent.tool_name,
                        "tool_input": self._sanitize_hook_value(
                            dict(intent.arguments),
                            trace,
                        ),
                        "tool_response": safe_output,
                        "success": intent.execution_success,
                        "error": (
                            self._sanitize_hook_value(
                                dict(intent.error),
                                trace,
                            )
                            if intent.error is not None
                            else None
                        ),
                        "identity": intent.identity,
                    },
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            trace.append(
                ToolPolicyTraceEntry(
                    step="post_tool_use_hook",
                    disposition="failed_open",
                    detail="timeout",
                )
            )
            return safe_output, False
        except Exception as exc:  # noqa: BLE001 -- advisory extension fails open
            trace.append(
                ToolPolicyTraceEntry(
                    step="post_tool_use_hook",
                    disposition="failed_open",
                    detail=type(exc).__name__,
                )
            )
            return safe_output, False

        output = safe_output
        if isinstance(outcome.updated_response, str):
            output = outcome.updated_response
            trace.append(
                ToolPolicyTraceEntry(
                    step="post_tool_use_hook",
                    disposition="rewrite",
                )
            )
        if outcome.behavior == "deny" or outcome.stop is not None:
            reason = (
                outcome.system_message
                or (outcome.stop.reason if outcome.stop is not None else "")
                or "tool result withheld by presentation policy"
            )
            trace.append(
                ToolPolicyTraceEntry(
                    step="post_tool_use_hook",
                    disposition="stop" if outcome.stop is not None else "deny",
                    detail="hook withheld result",
                )
            )
            return f"[PostToolUse] {reason}", outcome.stop is not None
        if outcome.additional_context:
            extra = "\n".join(outcome.additional_context)
            output = f"{output}\n{extra}" if output else extra
            trace.append(
                ToolPolicyTraceEntry(
                    step="post_tool_use_hook",
                    disposition="enrich",
                )
            )
        return output, False

    def _sanitize_hook_value(
        self,
        value,
        trace: list[ToolPolicyTraceEntry],
    ):
        if isinstance(value, str):
            return self._redact(value, trace, step="hook_input_redaction")
        if isinstance(value, dict):
            return {key: self._sanitize_hook_value(item, trace) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_hook_value(item, trace) for item in value]
        if isinstance(value, tuple):
            return tuple(self._sanitize_hook_value(item, trace) for item in value)
        return value

    def _apply_loop_guard(
        self,
        intent: ToolResultIntent,
        output: str,
        trace: list[ToolPolicyTraceEntry],
    ) -> str:
        detector = self._loop_detector
        if detector is None:
            return output
        signature = _tool_call_signature(intent.tool_name, dict(intent.arguments))
        fingerprint = content_hash(output) if intent.is_readonly and intent.execution_success else ""
        verdict = detector.record(
            tool_name=intent.tool_name,
            sig=signature,
            success=intent.execution_success,
            is_readonly=intent.is_readonly,
            result_fingerprint=fingerprint,
        )
        if verdict is None:
            return output
        nudge = _render_loop_nudge(verdict)
        trace.append(ToolPolicyTraceEntry(step="loop_guard", disposition="enrich"))
        return f"{output}\n{nudge}" if output else nudge


def build_tool_result_policy(
    *,
    hook_manager: Optional[HookManager] = None,
    secret_store: Optional[SecretStore] = None,
    loop_guard_config: Optional[LoopGuardConfig] = None,
) -> DefaultToolResultPolicy:
    config = loop_guard_config or LoopGuardConfig()
    detector = (
        ThrashDetector(
            failure_threshold=config.failure_threshold,
            no_progress_threshold=config.no_progress_threshold,
        )
        if config.enabled
        else None
    )
    return DefaultToolResultPolicy(
        hook_manager=hook_manager,
        secret_store=secret_store,
        loop_detector=detector,
    )


def build_tool_call_policy(
    permission_config: Optional[PermissionConfig],
    *,
    role: ToolCapabilityProvider | None = None,
    hook_manager: Optional[HookManager] = None,
    extensions: tuple[ToolCallPolicyExtensionSpec, ...] = (),
    require_permission: bool = False,
    permission_engine: PermissionEngine | None = None,
) -> DefaultToolCallPolicy:
    """Build the domain policy at an explicit composition root."""

    permission_mode = (
        (permission_config or PermissionConfig(mode="bypass")).mode
        if permission_config is not None or require_permission
        else None
    )
    if permission_engine is None:
        permission_engine = build_permission_engine(
            permission_config,
            role=role,
            require_permission=require_permission,
        )
    return DefaultToolCallPolicy(
        hook_manager=hook_manager,
        permission_engine=permission_engine,
        permission_mode=permission_mode,
        extensions=extensions,
    )


def build_permission_engine(
    permission_config: Optional[PermissionConfig],
    *,
    role: ToolCapabilityProvider | None = None,
    require_permission: bool = False,
) -> PermissionEngine | None:
    """Build the one canonical permission state shared by governed effects."""
    if permission_config is None and not require_permission:
        return None
    config = permission_config or PermissionConfig(mode="bypass")
    get_cwd: Optional[Callable[[], str]] = None
    if role is not None:
        capabilities = role.tool_capabilities()
        get_cwd = capabilities.get("get_cwd")
    sandbox = SandboxGuard(config.sandbox, get_cwd=get_cwd) if config.sandbox is not None else None
    return PermissionEngine(
        mode=config.mode,
        store=RuleStore.from_config(config),
        sandbox=sandbox,
    )


__all__ = [
    "DEFAULT_TOOL_POLICY_TIMEOUT",
    "DefaultToolCallPolicy",
    "build_permission_engine",
    "DefaultToolResultPolicy",
    "build_tool_call_policy",
    "build_tool_result_policy",
]
