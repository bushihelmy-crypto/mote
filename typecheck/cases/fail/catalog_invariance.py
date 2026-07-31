from mote.contracts.ports.agent.catalog import SpawnableAgentCatalog


class ReviewReport:
    pass


def require_object_catalog(catalog: SpawnableAgentCatalog[object]) -> None:
    del catalog


def reject_invariant(
    review_catalog: SpawnableAgentCatalog[ReviewReport],
) -> None:
    require_object_catalog(review_catalog)
