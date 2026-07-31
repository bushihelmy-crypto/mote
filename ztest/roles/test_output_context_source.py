import pytest
from pydantic import BaseModel

from mote.contracts.output import OutputContractId
from mote.kernel.output import OutputContract, TypeAdapterOutputDecoder, text_output_contract
from mote.runtime.agent.components.output_context import OutputContractContextSource


class Report(BaseModel):
    count: int


@pytest.mark.asyncio
async def test_structured_contract_renders_ephemeral_schema_guidance():
    source = OutputContractContextSource(
        OutputContract(
            OutputContractId("test", "report", "1"),
            TypeAdapterOutputDecoder(Report),
        )
    )

    block = await source.render()

    assert source.save_to_context is False
    assert "test.report@1" in block
    assert '"count"' in block
    assert "Markdown fences" in block


@pytest.mark.asyncio
async def test_default_text_contract_self_suppresses():
    source = OutputContractContextSource(text_output_contract())

    assert await source.render() is None
