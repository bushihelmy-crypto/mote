import pytest

from mote.contracts.output import OutputBindingKind, OutputRepresentationCapabilities
from mote.kernel.output_binding import FINAL_OUTPUT_TOOL_NAME, negotiate_output_binding


def test_text_contract_uses_text_without_downgrade():
    decision = negotiate_output_binding(
        is_text=True,
        capabilities=OutputRepresentationCapabilities(supports_text=True),
    )

    assert decision.binding.kind is OutputBindingKind.TEXT
    assert decision.downgrade_reasons == ()


def test_structured_binding_prefers_native_schema():
    decision = negotiate_output_binding(
        is_text=False,
        capabilities=OutputRepresentationCapabilities(
            supports_native_schema=True,
            supports_semantic_tool=True,
            supports_prompted_json=True,
        ),
    )

    assert decision.binding.kind is OutputBindingKind.NATIVE_SCHEMA
    assert decision.downgrade_reasons == ()


def test_structured_binding_records_semantic_tool_downgrade():
    decision = negotiate_output_binding(
        is_text=False,
        capabilities=OutputRepresentationCapabilities(
            supports_semantic_tool=True,
            supports_prompted_json=True,
        ),
    )

    assert decision.binding.kind is OutputBindingKind.NATIVE_TOOL
    assert decision.binding.tool_name == FINAL_OUTPUT_TOOL_NAME
    assert decision.downgrade_reasons == ("native_schema_not_supported",)


def test_prompted_json_records_every_unavailable_stronger_binding():
    decision = negotiate_output_binding(
        is_text=False,
        capabilities=OutputRepresentationCapabilities(supports_prompted_json=True),
    )

    assert decision.binding.kind is OutputBindingKind.PROMPTED_JSON
    assert decision.downgrade_reasons == (
        "native_schema_not_supported",
        "semantic_tool_not_supported",
    )


def test_structured_contract_fails_when_channel_has_no_representation():
    with pytest.raises(ValueError, match="cannot represent structured output"):
        negotiate_output_binding(
            is_text=False,
            capabilities=OutputRepresentationCapabilities(),
        )
