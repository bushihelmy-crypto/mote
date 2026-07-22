import pytest

from mote.common.utils.role_utils import _all_readonly_device, _duplicate_prompt, check_duplicate_calls


class _FakeLLM:
    """Records the prompt it was asked with; returns a canned problem summary."""

    def __init__(self):
        self.seen_context = None

    async def aask(self, context):
        self.seen_context = context
        return "I keep re-observing the same screen."


def _observe(mode="fused"):
    return {"command_name": "DeviceUse", "args": {"action": "observe", "mode": mode}}


class TestDuplicatePromptLanguage:
    def test_fills_named_language(self):
        out = _duplicate_prompt("chinese")
        assert "chinese" in out
        assert "{language}" not in out

    def test_empty_falls_back(self):
        out = _duplicate_prompt("")
        assert "{language}" not in out
        assert "the user's language" in out

    def test_auto_falls_back(self):
        out = _duplicate_prompt("auto")
        assert "{language}" not in out
        assert "the user's language" in out


class TestReadonlyDeviceExemption:
    def test_all_observe_is_readonly(self):
        assert _all_readonly_device([_observe()]) is True

    def test_wait_and_list_apps_readonly(self):
        assert _all_readonly_device([{"command_name": "DeviceUse", "args": {"action": "wait"}}]) is True
        assert _all_readonly_device([{"command_name": "DeviceUse", "args": {"action": "list_apps"}}]) is True

    def test_default_action_is_observe(self):
        assert _all_readonly_device([{"command_name": "DeviceUse", "args": {}}]) is True

    def test_mutating_action_not_readonly(self):
        assert _all_readonly_device([{"command_name": "DeviceUse", "args": {"action": "tap", "ref": "@e1"}}]) is False

    def test_non_device_tool_not_readonly(self):
        assert _all_readonly_device([{"command_name": "Read", "args": {"path": "x"}}]) is False

    def test_empty_not_readonly(self):
        assert _all_readonly_device([]) is False


@pytest.mark.asyncio
class TestCheckDuplicateCalls:
    async def test_repeated_observe_not_flagged(self):
        """Regression: repeated DeviceUse observe must NOT trip duplicate detection."""
        llm = _FakeLLM()
        sig_hist = [_sig() for _ in range(5)]  # observe already repeated many times
        override = await check_duplicate_calls(
            req=[], command_calls=[_observe()], sig_hist=sig_hist, llm=llm, language="chinese"
        )
        assert override is None
        assert llm.seen_context is None  # never asked the human

    async def test_repeated_mutating_call_is_flagged(self):
        llm = _FakeLLM()
        tap = {"command_name": "DeviceUse", "args": {"action": "tap", "x": 1, "y": 2}}
        from mote.common.utils.role_utils import call_signature

        sig_hist = [call_signature([tap]) for _ in range(3)]
        override = await check_duplicate_calls(
            req=[], command_calls=[tap], sig_hist=sig_hist, llm=llm, language="chinese"
        )
        assert override is not None
        assert override[0]["command_name"] == "AskUserQuestion"
        # The synthesized guidance prompt must be language-substituted, never raw.
        content = llm.seen_context[-1].content
        assert "{language}" not in content
        assert "chinese" in content


def _sig():
    from mote.common.utils.role_utils import call_signature

    return call_signature([_observe()])
