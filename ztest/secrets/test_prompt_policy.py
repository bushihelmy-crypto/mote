"""PromptPolicy safety, extension authority, and Hook adapter tests."""

from __future__ import annotations

import pytest

from mote.contracts.conversation.prompt_policy import PromptIntent, PromptPolicyContribution
from mote.contracts.ports.conversation.prompt_policy import PromptPolicyExtensionSpec
from mote.runtime.hook.manager import HookManager
from mote.runtime.prompt import DefaultPromptPolicy, build_prompt_policy
from mote.runtime.secrets.cipher import AesGcmCipher
from mote.runtime.secrets.store import SecretStore

_RAW_SECRET = "future-proof-secret-value"


def _store(tmp_path) -> SecretStore:
    return SecretStore(
        AesGcmCipher(bytes(range(32))),
        vault_path=tmp_path / "vault.json",
    )


@pytest.mark.asyncio
async def test_hook_receives_only_safe_view_and_final_gate_redacts_enrichment(
    tmp_path,
):
    store = _store(tmp_path)
    manager = HookManager()
    seen: list[str] = []

    def enrich(hook_input):
        seen.append(hook_input.payload.prompt)
        return {"additionalContext": _RAW_SECRET}

    manager.register("UserPromptSubmit", enrich)
    policy = build_prompt_policy(hook_manager=manager, secret_store=store)

    decision = await policy.process(PromptIntent(prompt=f'use <secret name="token">{_RAW_SECRET}</secret> now'))

    assert decision.accepted is True
    assert seen == ["use <agent-vault:token> now"]
    assert decision.additional_context == ("<agent-vault:token>",)
    assert _RAW_SECRET not in repr(decision.trace)


@pytest.mark.asyncio
async def test_hook_can_deny_but_cannot_rewrite_safe_prompt(tmp_path):
    manager = HookManager()
    manager.register(
        "UserPromptSubmit",
        lambda _input: {
            "decision": "block",
            "systemMessage": "project policy denied prompt",
            "updatedInput": {"prompt": "must-not-apply"},
        },
    )
    policy = build_prompt_policy(hook_manager=manager, secret_store=_store(tmp_path))

    decision = await policy.process(PromptIntent(prompt="original"))

    assert decision.accepted is False
    assert decision.prompt == "original"
    assert decision.reason == "project policy denied prompt"


@pytest.mark.asyncio
async def test_upload_fails_closed_when_vault_is_not_available():
    policy = build_prompt_policy()

    decision = await policy.process(PromptIntent(prompt=f"token <secret>{_RAW_SECRET}</secret>"))

    assert decision.accepted is False
    assert _RAW_SECRET not in decision.prompt
    assert decision.terminate is True


@pytest.mark.asyncio
async def test_final_leak_check_failure_withholds_entire_prompt():
    class BrokenStore:
        def as_map(self):
            raise RuntimeError("vault unavailable")

    policy = DefaultPromptPolicy(secret_store=BrokenStore())

    decision = await policy.process(PromptIntent(prompt="ordinary text"))

    assert decision.accepted is False
    assert decision.prompt.startswith("[prompt withheld")
    assert decision.terminate is True


@pytest.mark.asyncio
async def test_extension_can_enrich_or_deny_but_has_no_rewrite_channel():
    class Enrich:
        async def evaluate(self, intent):
            assert intent.prompt == "safe prompt"
            return PromptPolicyContribution.enrich("organization context")

    class Deny:
        async def evaluate(self, intent):
            return PromptPolicyContribution.deny("organization denied prompt")

    policy = build_prompt_policy(
        extensions=(
            PromptPolicyExtensionSpec("enrich", Enrich),
            PromptPolicyExtensionSpec("deny", Deny),
        )
    )

    decision = await policy.process(PromptIntent(prompt="safe prompt"))

    assert decision.accepted is False
    assert decision.prompt == "safe prompt"
    assert decision.additional_context == ("organization context",)
    assert decision.reason == "organization denied prompt"


@pytest.mark.asyncio
async def test_extension_failure_is_fail_closed():
    class Broken:
        async def evaluate(self, intent):
            raise RuntimeError("broken")

    policy = build_prompt_policy(extensions=(PromptPolicyExtensionSpec("organization", Broken),))

    decision = await policy.process(PromptIntent(prompt="safe prompt"))

    assert decision.accepted is False
    assert "denied for safety" in decision.reason
    assert decision.trace[-1].disposition == "failed_closed"


def test_extension_manifest_is_sealed_and_validated():
    class Neutral:
        async def evaluate(self, intent):
            return PromptPolicyContribution()

    spec = PromptPolicyExtensionSpec("organization", Neutral)
    policy = build_prompt_policy(extensions=(spec,))
    assert policy.manifest == (spec,)

    with pytest.raises(ValueError, match="duplicate"):
        build_prompt_policy(extensions=(spec, spec))
