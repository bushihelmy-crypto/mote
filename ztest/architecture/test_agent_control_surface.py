from __future__ import annotations

from pathlib import Path

from mote.product.interaction.ports import DriverControlDisposition, DriverControlReceipt

ROOT = Path(__file__).resolve().parents[2]


def test_presentation_ports_do_not_own_async_control_tasks() -> None:
    offenders: list[str] = []
    for relative in (
        "product/interfaces/terminal/port.py",
        "product/interfaces/textual/port.py",
        "product/interfaces/acp/port.py",
        "product/interfaces/agui/port.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        if "ensure_future(result)" in source or "create_task(result)" in source:
            offenders.append(relative)

    assert offenders == []


def test_driver_control_receipt_is_closed_and_typed() -> None:
    assert tuple(DriverControlDisposition) == (
        DriverControlDisposition.ACCEPTED,
        DriverControlDisposition.ALREADY_PENDING,
        DriverControlDisposition.IGNORED,
    )
    assert DriverControlReceipt(DriverControlDisposition.ACCEPTED).disposition is DriverControlDisposition.ACCEPTED
