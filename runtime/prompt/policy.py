"""Sealed prompt admission policy executed before history or model exposure."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

from mote.contracts.conversation.prompt_policy import (
    PromptDecision,
    PromptIntent,
    PromptPolicyContribution,
    PromptPolicyTraceEntry,
)
from mote.contracts.ports.conversation.prompt_policy import PromptPolicyExtension, PromptPolicyExtensionSpec
from mote.runtime.hook.manager import HookManager
from mote.runtime.secrets.policy import redact
from mote.runtime.secrets.store import SecretStore

DEFAULT_PROMPT_POLICY_TIMEOUT = 120.0
_UPLOAD_RE = re.compile(
    r'<secret(?:\s+name="([^"]*)")?\s*>(.+?)</secret>',
    re.DOTALL,
)
_DANGLING_RE = re.compile(r"<secret\b.*\Z", re.DOTALL)
_DANGLING_MASK = "<agent-vault:unterminated>"
_UNAVAILABLE_MASK = "<agent-vault:unavailable>"
_WITHHELD_PROMPT = "[prompt withheld because the secret-protection policy was unavailable]"


@dataclass(frozen=True)
class _InstalledExtension:
    spec: PromptPolicyExtensionSpec
    extension: PromptPolicyExtension


class DefaultPromptPolicy:
    """Capture secrets → safe extensions → final leak-check policy."""

    def __init__(
        self,
        *,
        hook_manager: Optional[HookManager] = None,
        secret_store: Optional[SecretStore] = None,
        extensions: tuple[PromptPolicyExtensionSpec, ...] = (),
        timeout: float = DEFAULT_PROMPT_POLICY_TIMEOUT,
    ) -> None:
        if timeout <= 0:
            raise ValueError("prompt policy timeout must be positive")
        self._hook_manager = hook_manager
        self._secret_store = secret_store
        self._manifest, self._extensions = self._install_extensions(extensions)
        self._timeout = timeout

    @staticmethod
    def _install_extensions(
        extensions: tuple[PromptPolicyExtensionSpec, ...],
    ) -> tuple[tuple[PromptPolicyExtensionSpec, ...], tuple[_InstalledExtension, ...],]:
        sealed = tuple(extensions)
        identities: set[str] = set()
        installed: list[_InstalledExtension] = []
        for spec in sealed:
            if not spec.identity or not spec.identity.strip():
                raise ValueError("prompt policy extension identity must not be empty")
            if spec.identity in identities:
                raise ValueError(f"duplicate prompt policy extension identity: {spec.identity}")
            if spec.timeout <= 0:
                raise ValueError(f"prompt policy extension timeout must be positive: {spec.identity}")
            identities.add(spec.identity)
            extension = spec.factory()
            if not callable(getattr(extension, "evaluate", None)):
                raise TypeError("prompt policy extension factory must return an evaluator: " f"{spec.identity}")
            installed.append(_InstalledExtension(spec, extension))
        return sealed, tuple(installed)

    @property
    def manifest(self) -> tuple[PromptPolicyExtensionSpec, ...]:
        """The sealed, deterministic extension roster."""

        return self._manifest

    async def process(self, intent: PromptIntent) -> PromptDecision:
        trace: list[PromptPolicyTraceEntry] = []
        if not isinstance(intent.prompt, str):
            trace.append(
                PromptPolicyTraceEntry(
                    step="normalize_prompt",
                    disposition="failed_closed",
                    detail="invalid prompt type",
                )
            )
            return PromptDecision.reject(
                _WITHHELD_PROMPT,
                "prompt must be text",
                trace=tuple(trace),
            )

        prompt, capture_error = self._capture_and_vault(intent.prompt, trace)
        if capture_error is not None:
            return PromptDecision.reject(
                prompt,
                capture_error,
                terminate=True,
                trace=tuple(trace),
            )

        additional_context: list[str] = []
        denied_reason = ""
        terminate = False

        hook_reason, hook_terminate = await self._apply_hook(
            prompt,
            additional_context,
            trace,
        )
        if hook_reason:
            denied_reason = hook_reason
            terminate = hook_terminate
        else:
            denied_reason = await self._apply_extensions(
                prompt,
                additional_context,
                trace,
            )

        final = self._final_leak_check(
            prompt,
            additional_context,
            denied_reason,
            trace,
        )
        if final is None:
            return PromptDecision.reject(
                _WITHHELD_PROMPT,
                "prompt leak-check policy was unavailable; denied for safety.",
                terminate=True,
                trace=tuple(trace),
            )
        prompt, safe_context, safe_reason = final

        if denied_reason:
            return PromptDecision.reject(
                prompt,
                safe_reason,
                additional_context=safe_context,
                terminate=terminate,
                trace=tuple(trace),
            )

        trace.append(PromptPolicyTraceEntry(step="final_admission", disposition="allow"))
        return PromptDecision.accept(
            prompt,
            additional_context=safe_context,
            trace=tuple(trace),
        )

    def _capture_and_vault(
        self,
        prompt: str,
        trace: list[PromptPolicyTraceEntry],
    ) -> tuple[str, Optional[str]]:
        store = self._secret_store
        has_upload = _UPLOAD_RE.search(prompt) is not None
        has_dangling = _DANGLING_RE.search(_UPLOAD_RE.sub("", prompt)) is not None
        if store is None:
            if has_upload or has_dangling:
                trace.append(
                    PromptPolicyTraceEntry(
                        step="capture_and_vault_secrets",
                        disposition="failed_closed",
                        detail="vault unavailable",
                    )
                )
                return self._mask_secret_markup(prompt), (
                    "secret upload requires an available vault; denied for safety."
                )
            return prompt, None

        try:
            uploaded = False

            def replace_upload(match: re.Match[str]) -> str:
                nonlocal uploaded
                uploaded = True
                name, value = match.group(1), match.group(2)
                if name:
                    return store.add_user_secret(name, value)
                return store.add_session_secret(value)

            safe_prompt = _UPLOAD_RE.sub(replace_upload, prompt)
            if _DANGLING_RE.search(safe_prompt):
                safe_prompt = _DANGLING_RE.sub(_DANGLING_MASK, safe_prompt)
                trace.append(
                    PromptPolicyTraceEntry(
                        step="capture_and_vault_secrets",
                        disposition="failed_closed",
                        detail="unterminated secret upload",
                    )
                )
                return safe_prompt, ("Unterminated <secret> tag; denied to avoid leaking a partial secret.")
            safe_prompt, hits = redact(safe_prompt, store.as_map())
        except Exception as exc:  # noqa: BLE001 -- disclosure boundary fails closed
            trace.append(
                PromptPolicyTraceEntry(
                    step="capture_and_vault_secrets",
                    disposition="failed_closed",
                    detail=type(exc).__name__,
                )
            )
            return _WITHHELD_PROMPT, ("secret-protection policy was unavailable; denied for safety.")

        if uploaded or hits:
            trace.append(
                PromptPolicyTraceEntry(
                    step="capture_and_vault_secrets",
                    disposition="redact",
                )
            )
        return safe_prompt, None

    async def _apply_hook(
        self,
        safe_prompt: str,
        additional_context: list[str],
        trace: list[PromptPolicyTraceEntry],
    ) -> tuple[str, bool]:
        manager = self._hook_manager
        if manager is None:
            return "", False
        try:
            outcome = await asyncio.wait_for(
                manager.fire("UserPromptSubmit", {"prompt": safe_prompt}),
                timeout=self._timeout,
            )
            context = self._validated_context(outcome.additional_context)
        except asyncio.TimeoutError:
            trace.append(
                PromptPolicyTraceEntry(
                    step="user_prompt_submit_hook",
                    disposition="failed_open",
                    detail="timeout",
                )
            )
            return "", False
        except Exception as exc:  # noqa: BLE001 -- advisory adapter fails open
            trace.append(
                PromptPolicyTraceEntry(
                    step="user_prompt_submit_hook",
                    disposition="failed_open",
                    detail=type(exc).__name__,
                )
            )
            return "", False

        if context:
            additional_context.extend(context)
            trace.append(
                PromptPolicyTraceEntry(
                    step="user_prompt_submit_hook",
                    disposition="enrich",
                )
            )
        if outcome.behavior == "deny" or outcome.stop:
            trace.append(
                PromptPolicyTraceEntry(
                    step="user_prompt_submit_hook",
                    disposition="deny",
                    detail="hook denied prompt",
                )
            )
            return (
                outcome.system_message or outcome.stop_reason or "prompt denied by UserPromptSubmit hook",
                outcome.stop,
            )
        return "", False

    async def _apply_extensions(
        self,
        safe_prompt: str,
        additional_context: list[str],
        trace: list[PromptPolicyTraceEntry],
    ) -> str:
        intent = PromptIntent(prompt=safe_prompt)
        for installed in self._extensions:
            spec = installed.spec
            step = f"extension:{spec.identity}"
            try:
                contribution = await asyncio.wait_for(
                    installed.extension.evaluate(intent),
                    timeout=min(spec.timeout, self._timeout),
                )
                if not isinstance(contribution, PromptPolicyContribution):
                    raise TypeError("extension returned an invalid contribution")
                context = self._validated_context(contribution.additional_context)
            except asyncio.TimeoutError:
                trace.append(
                    PromptPolicyTraceEntry(
                        step=step,
                        disposition="failed_closed",
                        detail="timeout",
                    )
                )
                return f"prompt policy extension '{spec.identity}' timed out; " "denied for safety."
            except Exception as exc:  # noqa: BLE001 -- organization gate fails closed
                trace.append(
                    PromptPolicyTraceEntry(
                        step=step,
                        disposition="failed_closed",
                        detail=type(exc).__name__,
                    )
                )
                return f"prompt policy extension '{spec.identity}' failed; " "denied for safety."

            if context:
                additional_context.extend(context)
                trace.append(PromptPolicyTraceEntry(step=step, disposition="enrich"))
            if not contribution.allowed:
                trace.append(
                    PromptPolicyTraceEntry(
                        step=step,
                        disposition="deny",
                        detail="extension denied prompt",
                    )
                )
                return contribution.reason or (f"prompt policy extension '{spec.identity}' denied the prompt")
            if not context:
                trace.append(PromptPolicyTraceEntry(step=step, disposition="allow"))
        return ""

    def _final_leak_check(
        self,
        prompt: str,
        additional_context: list[str],
        denied_reason: str,
        trace: list[PromptPolicyTraceEntry],
    ) -> Optional[tuple[str, tuple[str, ...], str]]:
        store = self._secret_store
        if store is None:
            return prompt, tuple(additional_context), denied_reason
        try:
            secrets = store.as_map()
            safe_prompt, prompt_hits = redact(prompt, secrets)
            safe_context: list[str] = []
            context_hits: list[str] = []
            for item in additional_context:
                safe_item, hits = redact(item, secrets)
                safe_context.append(safe_item)
                context_hits.extend(hits)
            safe_reason, reason_hits = redact(denied_reason, secrets)
        except Exception as exc:  # noqa: BLE001 -- final disclosure gate fails closed
            trace.append(
                PromptPolicyTraceEntry(
                    step="final_leak_check",
                    disposition="failed_closed",
                    detail=type(exc).__name__,
                )
            )
            return None
        if prompt_hits or context_hits or reason_hits:
            trace.append(
                PromptPolicyTraceEntry(
                    step="final_leak_check",
                    disposition="redact",
                )
            )
        return safe_prompt, tuple(safe_context), safe_reason

    @staticmethod
    def _validated_context(context) -> tuple[str, ...]:
        values = tuple(context)
        if any(not isinstance(item, str) for item in values):
            raise TypeError("additional context must contain only strings")
        return values

    @staticmethod
    def _mask_secret_markup(prompt: str) -> str:
        masked = _UPLOAD_RE.sub(_UNAVAILABLE_MASK, prompt)
        return _DANGLING_RE.sub(_DANGLING_MASK, masked)


def build_prompt_policy(
    *,
    hook_manager: Optional[HookManager] = None,
    secret_store: Optional[SecretStore] = None,
    extensions: tuple[PromptPolicyExtensionSpec, ...] = (),
) -> DefaultPromptPolicy:
    """Build one sealed PromptPolicy at the Role composition root."""

    return DefaultPromptPolicy(
        hook_manager=hook_manager,
        secret_store=secret_store,
        extensions=extensions,
    )


__all__ = [
    "DEFAULT_PROMPT_POLICY_TIMEOUT",
    "DefaultPromptPolicy",
    "build_prompt_policy",
]
