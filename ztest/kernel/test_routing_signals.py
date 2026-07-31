from mote.kernel.inference import build_routing_signals


def test_build_routing_signals_normalizes_messages_and_counts_turns():
    signals = build_routing_signals(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
    )

    assert signals.conversation_turns == 2
    assert signals.prompt_text == "first\nanswer\nsecond"
    assert signals.estimated_tokens > 0
