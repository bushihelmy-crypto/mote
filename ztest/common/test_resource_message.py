"""ResourceMessage: type-as-shell + metadata-as-truth round-trip."""

from mote.contracts.conversation import Message, ResourceMessage, dump_message, load_message
from mote.contracts.conversation.fields import RESOURCE_ID, RESOURCE_KIND, RESOURCE_STICKY
from mote.runtime.models.message_wire import message_to_model_wire


def test_resource_message_is_user_role_with_metadata_truth():
    m = ResourceMessage("BODY", resource_id="simplify", resource_kind="skill", sticky=True)
    assert m.role == "user"
    assert m.content == "BODY"
    assert m.metadata[RESOURCE_ID] == "simplify"
    assert m.metadata[RESOURCE_KIND] == "skill"
    assert m.metadata[RESOURCE_STICKY] is True
    # ergonomic accessors mirror the metadata truth
    assert m.resource_id == "simplify"
    assert m.resource_kind == "skill"
    assert m.is_sticky is True


def test_defaults_kind_skill_and_sticky_true():
    m = ResourceMessage("B", resource_id="x")
    assert m.resource_kind == "skill"
    assert m.is_sticky is True


def test_to_dict_is_plain_user_message():
    # On the wire it is an ordinary user message; the resource facts ride in
    # metadata (which to_dict does not surface), not in a special role.
    m = ResourceMessage("BODY", resource_id="simplify")
    d = message_to_model_wire(m)
    assert d == {"role": "user", "content": "BODY"}


def test_metadata_survives_dump_load_even_though_subclass_is_lost():
    # Message.load reconstructs via base Message.from_dict (cls(**m)): the
    # ResourceMessage subclass identity is gone, but the metadata truth remains
    # so every consumer can key off RESOURCE_* rather than isinstance.
    m = ResourceMessage("BODY", resource_id="simplify", resource_kind="skill", sticky=True)
    restored = load_message(dump_message(m))
    assert restored is not None
    assert type(restored) is Message  # subclass lost on replay
    assert restored.role == "user"
    assert restored.content == "BODY"
    assert restored.metadata[RESOURCE_ID] == "simplify"
    assert restored.metadata[RESOURCE_KIND] == "skill"
    assert restored.metadata[RESOURCE_STICKY] is True
