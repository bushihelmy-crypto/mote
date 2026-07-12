#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end secret redaction/upload through the control-plane seam.

Proves the productionized secret system on the *real* control plane, in both
directions, over the encrypted two-section vault:

1. ``policy.redact`` in isolation — the guards (min length, placeholders).
2. The OUTPUT bus fold — a real ``EventBus`` + ``RedactionSubscriber`` folds a
   redacted ``updated_response`` out of a ``PostToolUseEvent`` (the
   ``cat config.yaml``-style leak the seam must catch).
3. The INPUT bus fold — ``SecretUploadSubscriber`` vaults ``<secret>…</secret>``
   spans in a ``UserPromptSubmitEvent`` and rewrites the prompt before it leaves the
   seam.
4. The real ``ToolExecutor.run_command`` — an echo tool leaks a config api_key;
   the model-facing ``result.output`` comes back redacted, no per-tool changes.
"""
from __future__ import annotations

import pytest

from mote.common.events import EventBus, PostToolUseEvent, UserPromptSubmitEvent
from mote.common.secrets.cipher import AesGcmCipher
from mote.common.secrets.policy import MIN_REDACT_LENGTH, redact
from mote.common.secrets.store import SecretStore
from mote.executor.base_tool import BaseTool
from mote.executor.secrets.subscriber import RedactionSubscriber, SecretUploadSubscriber
from mote.executor.tool_executor import ToolExecutor

# A realistic-looking secret value, comfortably above MIN_REDACT_LENGTH.
_API_KEY = "sk-proj-abc123SUPERsecretVALUE456"


def _cipher() -> AesGcmCipher:
    return AesGcmCipher(bytes(range(32)))


def _config_store(tmp_path, api_key: str = _API_KEY) -> SecretStore:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"llm:\n  api_key: {api_key}\n")
    return SecretStore(_cipher(), vault_path=tmp_path / "vault.json", config_path=cfg)


# ---------------------------------------------------------------------------
# 1. Pure policy
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_redacts_known_value(self):
        text = f"OPENAI_API_KEY={_API_KEY}\nPORT=3000"
        out, hits = redact(text, {_API_KEY: "<secret:llm.api_key>"})
        assert _API_KEY not in out
        assert "<secret:llm.api_key>" in out
        assert "PORT=3000" in out  # non-secret untouched
        assert hits == ["<secret:llm.api_key>"]

    def test_short_values_are_not_redacted(self):
        assert len("3000") < MIN_REDACT_LENGTH
        out, hits = redact("PORT=3000", {"3000": "<secret:port>"})
        assert out == "PORT=3000"
        assert hits == []

    def test_placeholder_values_skipped(self):
        out, hits = redact("key: sk-", {"sk-": "<secret:x>"})
        assert out == "key: sk-"
        assert hits == []


# ---------------------------------------------------------------------------
# 2. OUTPUT bus fold (real EventBus + RedactionSubscriber)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOutputBusFold:
    async def test_post_tool_use_output_is_redacted(self, tmp_path):
        bus = EventBus()
        bus.subscribe(RedactionSubscriber(_config_store(tmp_path)))

        event = PostToolUseEvent(
            tool_name="Bash",
            tool_input={"command": "cat config.yaml"},
            tool_response=f"api_key: {_API_KEY}\nport: 3000",
        )
        outcome = await bus.emit(event)

        assert outcome is not None
        assert outcome.updated_response is not None
        assert _API_KEY not in outcome.updated_response
        assert "<secret:llm.api_key>" in outcome.updated_response

    async def test_no_rewrite_when_output_clean(self, tmp_path):
        bus = EventBus()
        bus.subscribe(RedactionSubscriber(_config_store(tmp_path)))

        event = PostToolUseEvent(tool_name="Bash", tool_response="port: 3000\nhost: localhost")
        outcome = await bus.emit(event)
        assert outcome is None or outcome.updated_response is None


# ---------------------------------------------------------------------------
# 3. INPUT bus fold (real EventBus + SecretUploadSubscriber)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInputBusFold:
    async def test_named_upload_vaulted_and_prompt_rewritten(self, tmp_path):
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        event = UserPromptSubmitEvent(prompt='use <secret name="TG">1234567890:AAtokenvalue</secret> to post')
        outcome = await bus.emit(event)

        assert outcome is not None
        assert outcome.updated_prompt == "use <agent-vault:TG> to post"
        # The raw value was vaulted (persisted) — a fresh store recovers it.
        reloaded = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        assert "1234567890:AAtokenvalue" in reloaded.as_map()

    async def test_observer_sees_rewritten_prompt(self, tmp_path):
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        event = UserPromptSubmitEvent(prompt="anon <secret>my-anonymous-secret-value</secret> here")
        # ``emit`` returns the folded outcome; the rewritten event is threaded to
        # any later subscriber/observer — assert the prompt no longer leaks.
        outcome = await bus.emit(event)
        assert "my-anonymous-secret-value" not in outcome.updated_prompt
        assert "<agent-vault:session-" in outcome.updated_prompt

    async def test_value_containing_at_is_captured_whole(self, tmp_path):
        # An email value contains an ``@``; the explicit ``</secret>`` close captures
        # it whole, so nothing is truncated at the email's own ``@`` and leaked.
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        outcome = await bus.emit(UserPromptSubmitEvent(prompt="mail <secret>user@example.com</secret> ok"))
        assert outcome is not None
        assert "user@example.com" not in outcome.updated_prompt
        assert "example.com" not in outcome.updated_prompt  # no truncated leak
        assert "<agent-vault:session-" in outcome.updated_prompt
        assert "user@example.com" in store.as_map()  # full value vaulted

    async def test_named_email_value_persisted_whole(self, tmp_path):
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        outcome = await bus.emit(
            UserPromptSubmitEvent(prompt='reset <secret name="email">user@example.com</secret> now')
        )
        assert outcome.updated_prompt == "reset <agent-vault:email> now"
        reloaded = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        assert reloaded.as_map().get("user@example.com") == "<agent-vault:email>"

    async def test_value_with_spaces_and_equals_captured_whole(self, tmp_path):
        # The XML close means the value may contain spaces and ``=`` (a connection
        # string) with no ambiguity — this is what the old ``@``-fence could not do.
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        raw = "Server=db;User Id=admin;Password=p@ss w0rd="
        outcome = await bus.emit(UserPromptSubmitEvent(prompt=f'db <secret name="dsn">{raw}</secret> end'))
        assert outcome.updated_prompt == "db <agent-vault:dsn> end"
        assert raw in store.as_map()

    async def test_two_spans_not_merged(self, tmp_path):
        # Non-greedy ``</secret>`` close keeps two spans distinct even when the first
        # value contains an ``@`` (a greedy close would swallow both).
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        outcome = await bus.emit(
            UserPromptSubmitEvent(
                prompt='a <secret name="e">user@x.com</secret> b <secret>second-anon-value</secret> c'
            )
        )
        assert "user@x.com" not in outcome.updated_prompt
        assert "second-anon-value" not in outcome.updated_prompt
        assert outcome.updated_prompt.startswith("a <agent-vault:e> b <agent-vault:session-")
        assert outcome.updated_prompt.endswith(" c")
        assert outcome.updated_prompt.count("<agent-vault:") == 2
        assert "user@x.com" in store.as_map()

    async def test_unterminated_tag_stops_and_masks(self, tmp_path):
        # No ``</secret>`` close → the whole remainder (marker + half-typed secret)
        # is masked to end-of-string and the turn stops: a partial secret can't leak.
        store = SecretStore(_cipher(), vault_path=tmp_path / "vault.json")
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        event = UserPromptSubmitEvent(prompt="oops <secret>half-typed-no-close")
        outcome = await bus.emit(event)
        assert outcome.stop is True
        assert "half-typed-no-close" not in outcome.updated_prompt  # tail masked, not just marker
        assert "<secret" not in outcome.updated_prompt  # dangling opener masked

    async def test_clean_prompt_is_noop(self, tmp_path):
        store = _config_store(tmp_path)
        bus = EventBus()
        bus.subscribe(SecretUploadSubscriber(store))

        outcome = await bus.emit(UserPromptSubmitEvent(prompt="just a normal question"))
        assert outcome is None or outcome.updated_prompt is None


# ---------------------------------------------------------------------------
# 4. End-to-end through the real ToolExecutor
# ---------------------------------------------------------------------------


class _LeakyEcho(BaseTool):
    """A stand-in for any tool that surfaces file content (e.g. ``Bash cat``)."""

    name = "LeakyEcho"

    async def call(self, *, text: str = "") -> str:
        return text


@pytest.mark.asyncio
class TestExecutorEndToEnd:
    async def test_run_command_output_is_redacted(self, tmp_path):
        bus = EventBus()
        bus.subscribe(RedactionSubscriber(_config_store(tmp_path)))

        ex = ToolExecutor("sess", tools=None, bus=bus)
        tool = _LeakyEcho()
        tool.bind("sess")
        ex.register_tool_instance(tool, [tool.name])

        result = await ex.run_command("LeakyEcho", {"text": f"api_key: {_API_KEY}"})

        assert result.success is True
        assert _API_KEY not in result.output
        assert "<secret:llm.api_key>" in result.output
