"""Output operations reject a transaction with a different output type."""

from typing import cast

from mote.contracts.ports.execution.transaction import ExecutionOutputTransactionPort
from mote.contracts.ports.output.evaluation import OutputEngine
from mote.kernel.execution.context import ExecutionContext
from mote.kernel.execution.operations.output import OutputOperation

OutputOperation[int](
    context=lambda: cast(ExecutionContext, None),
    channel=lambda: None,
    inference_engine=None,
    transaction=cast(ExecutionOutputTransactionPort[str], None),
    output_engine=cast(OutputEngine[int], None),
    report_inference_result=lambda result: None,
)
