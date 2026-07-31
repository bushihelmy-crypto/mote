from mote.kernel.execution.engine import RUN_EVENT_BUFFER_SIZE
from mote.kernel.execution.limits import DEFAULT_EXECUTION_LIMITS


def test_kernel_limits_contain_only_algorithmic_safety_bounds():
    assert set(DEFAULT_EXECUTION_LIMITS.__dataclass_fields__) == {
        "graph_transitions",
        "run_event_buffer",
    }
    assert RUN_EVENT_BUFFER_SIZE == DEFAULT_EXECUTION_LIMITS.run_event_buffer
