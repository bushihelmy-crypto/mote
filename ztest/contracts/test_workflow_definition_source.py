import hashlib

import pytest

from mote.contracts.workflow import (
    DeclarativeWorkflowDefinitionSource,
    TrustedWorkflowBlueprintSource,
    decode_workflow_definition_source,
    encode_workflow_definition_source,
)


def test_definition_source_tagged_union_round_trips_both_variants() -> None:
    payload = '{"schema":"graph/v1"}'
    sources = (
        DeclarativeWorkflowDefinitionSource(
            "mote.product.run-graph",
            1,
            payload,
            hashlib.sha256(payload.encode()).hexdigest(),
        ),
        TrustedWorkflowBlueprintSource("product.report", 2),
    )
    assert (
        tuple(decode_workflow_definition_source(encode_workflow_definition_source(source)) for source in sources)
        == sources
    )


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"schema": "mote.workflow-definition-source/v1", "kind": "unknown"},
        {
            "schema": "mote.workflow-definition-source/v1",
            "kind": "trusted_blueprint",
            "blueprint_id": "product.report",
            "blueprint_version": True,
        },
        {
            "schema": "mote.workflow-definition-source/v1",
            "kind": "trusted_blueprint",
            "blueprint_id": "product.report",
            "blueprint_version": 1,
            "extra": False,
        },
    ],
)
def test_definition_source_decoder_fails_closed(raw) -> None:
    with pytest.raises(ValueError):
        decode_workflow_definition_source(raw)


@pytest.mark.parametrize("payload", ['{"value":NaN}', "[]", '{"b":1,"a":2}'])
def test_declarative_definition_source_requires_strict_canonical_object(
    payload: str,
) -> None:
    with pytest.raises(ValueError):
        DeclarativeWorkflowDefinitionSource(
            "mote.product.run-graph",
            1,
            payload,
            hashlib.sha256(payload.encode()).hexdigest(),
        )
