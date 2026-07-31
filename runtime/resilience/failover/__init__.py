"""Provider-neutral failover policy and availability primitives."""

from mote.runtime.resilience.failover.classification import classify_failure
from mote.runtime.resilience.failover.policy import DefaultFailoverPolicy, FailoverPolicy

__all__ = ["DefaultFailoverPolicy", "FailoverPolicy", "classify_failure"]
