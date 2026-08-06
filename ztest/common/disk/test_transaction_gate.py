from __future__ import annotations

import threading

from mote.runtime.persistence.transaction_gate import SerialTransactionGate


def test_serial_transaction_gate_protects_the_caller_defined_scope() -> None:
    gate = SerialTransactionGate()
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with gate.transaction():
            entered.set()
            assert release.wait(timeout=2)

    def second() -> None:
        assert entered.wait(timeout=2)
        with gate.transaction():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.05)
    release.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered.is_set()
