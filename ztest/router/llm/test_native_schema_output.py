from mote.product.integrations.models.openai_chat import OpenAILLM


def test_openai_profile_builds_strict_json_schema_request():
    llm = OpenAILLM.__new__(OpenAILLM)
    llm.model = "gpt-4o"
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }

    request = llm.native_schema_request(schema)

    assert request == {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "mote_output",
                "strict": True,
                "schema": {
                    **schema,
                    "required": ["count"],
                    "additionalProperties": False,
                },
            },
        }
    }


def test_unknown_openai_compatible_model_does_not_claim_schema_support():
    llm = OpenAILLM.__new__(OpenAILLM)
    llm.model = "unknown-gateway-model"

    assert llm.supports_native_schema_output() is False
    assert llm.native_schema_request({"type": "string"}) is None


def test_openai_strict_schema_rejects_open_ended_maps_without_mutation():
    import pytest

    llm = OpenAILLM.__new__(OpenAILLM)
    llm.model = "gpt-4o"
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}

    with pytest.raises(ValueError, match="open-ended object map"):
        llm.native_schema_request(schema)

    assert schema == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }
