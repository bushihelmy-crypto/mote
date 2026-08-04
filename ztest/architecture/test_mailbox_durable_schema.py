from __future__ import annotations

import inspect

from mote.orchestration.agents.messaging.mailbox import Mailbox
from mote.orchestration.agents.residency.codec import RESIDENCY_SCHEMA
from mote.orchestration.agents.residency.model import ResidencyRecord


def test_mailbox_is_a_process_projection_not_a_durable_authority() -> None:
    assert "dump" not in set(dir(Mailbox))
    assert "load" not in set(dir(Mailbox))
    assert "mote.agent-mailbox/" not in inspect.getsource(Mailbox)


def test_residency_v2_excludes_mailbox_truth() -> None:
    assert RESIDENCY_SCHEMA == "mote.agent-residency/v2"
    assert "mailbox_snapshot" not in ResidencyRecord.__dataclass_fields__
