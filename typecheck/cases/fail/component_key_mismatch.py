from mote.runtime.agent.component_graph import ComponentGraph, ComponentKey


class Role:
    pass


class State:
    pass


def reject_key_value(graph: ComponentGraph[Role, State], text_key: ComponentKey[str]) -> None:
    number: int = graph.get(text_key)
    del number
