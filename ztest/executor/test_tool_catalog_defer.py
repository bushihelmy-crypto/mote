"""Protocol-specific deferred-tool projection tests."""

from __future__ import annotations

import threading

import pytest

from mote.contracts.tool import ToolEffect
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.tool_binding import ExecutableToolBinding
from mote.runtime.tools.tool_catalog import NativeToolCatalog, XmlToolCatalog


class Capability:
    def __init__(self, name: str) -> None:
        self.name = name

    @staticmethod
    def resolve_effect() -> ToolEffect:
        return ToolEffect.PURE


def _xml_definition(name: str, description: str) -> XmlToolDefinition[Capability]:
    return XmlToolDefinition(
        name=name,
        capability_factory=lambda: Capability(name),
        capability_type=Capability,
        schema_renderer=lambda _: {
            "name": name,
            "description": description,
            "parameters": {"value": "string"},
        },
        source_identity=f"test:{name}",
        description=description,
        summary=description.splitlines()[0],
        search_text=description,
    )


def _native_definition(name: str, description: str, *, category: str = "builtin") -> NativeToolDefinition[Capability]:
    return NativeToolDefinition(
        name=name,
        capability_factory=lambda: Capability(name),
        capability_type=Capability,
        schema_renderer=lambda _: {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
        source_identity=f"test:{name}",
        description=description,
        summary=description.splitlines()[0],
        search_text=description,
        category=category,
    )


def _xml_catalog(*, revealed: set[str] | None = None) -> XmlToolCatalog:
    current = revealed or set()
    catalog = XmlToolCatalog(deferred={"Convert"}, get_revealed=lambda: current)
    for name, description in (
        ("Read", "Read a file."),
        ("Convert", "Convert an image."),
    ):
        definition = _xml_definition(name, description)
        catalog.register(ExecutableToolBinding(definition, Capability(name)), [name])
    return catalog


def _native_catalog(*, revealed: set[str] | None = None) -> NativeToolCatalog:
    current = revealed or set()
    catalog = NativeToolCatalog(deferred={"Convert"}, get_revealed=lambda: current)
    for name, description in (
        ("Read", "Read a file."),
        ("Convert", "Convert an image."),
    ):
        definition = _native_definition(name, description)
        catalog.register(ExecutableToolBinding(definition, Capability(name)), [name])
    return catalog


def test_xml_withholds_unrevealed_definition() -> None:
    assert set(_xml_catalog().schemas_for("builtin")) == {"Read"}
    assert set(_xml_catalog(revealed={"Convert"}).schemas_for("builtin")) == {
        "Read",
        "Convert",
    }


def test_native_withholds_unrevealed_tool_on_client_path() -> None:
    specs = _native_catalog().native_specs("openai", model="gpt-4o")
    assert {spec["function"]["name"] for spec in specs} == {"Read"}


def test_native_client_path_adds_full_schema_after_reveal() -> None:
    before = _native_catalog().native_specs("openai", model="gpt-4o")
    after = _native_catalog(revealed={"Convert"}).native_specs("openai", model="gpt-4o")
    assert {spec["function"]["name"] for spec in before} == {"Read"}
    convert = next(spec["function"] for spec in after if spec["function"]["name"] == "Convert")
    assert convert["description"] == "Convert an image."
    assert convert["parameters"]["properties"]


def test_native_dynamic_catalog_does_not_leak_hidden_schema() -> None:
    revealed: set[str] = set()
    catalog = NativeToolCatalog(deferred={"Convert"}, get_revealed=lambda: revealed)
    definition = _native_definition("Convert", "Convert an image.", category="mcp")
    catalog.register(ExecutableToolBinding(definition, Capability("Convert")), ["Convert"])

    assert catalog.schemas_for("mcp") == {}
    revealed.add("Convert")
    assert set(catalog.schemas_for("mcp")) == {"Convert"}


def test_native_server_defer_is_corpus_based_and_stable() -> None:
    before = _native_catalog().native_specs("anthropic", model="opus-4")
    after = _native_catalog(revealed={"Convert"}).native_specs("anthropic", model="opus-4")
    assert before == after
    convert = next(spec for spec in before if spec["name"] == "Convert")
    read = next(spec for spec in before if spec["name"] == "Read")
    assert convert["defer_loading"] is True
    assert "defer_loading" not in read


def test_deferred_menus_use_current_protocol_definition() -> None:
    catalog = _native_catalog()
    assert catalog.deferred_index() == {"Convert": "Convert an image."}
    assert catalog.deferred_search_index() == {"Convert": "Convert an image."}
    assert catalog.describe_deferred(["Convert"]) == {"Convert": "Convert an image."}
    assert catalog.split_tool_menu() == {"Convert": "Convert an image."}


def test_protocol_catalog_rejects_wrong_definition() -> None:
    xml = XmlToolCatalog()
    native_definition = _native_definition("Wrong", "Wrong protocol.")
    xml.register(ExecutableToolBinding(native_definition, Capability("Wrong")), ["Wrong"])
    try:
        xml.schemas_for(None)
    except TypeError as exc:
        assert "non-XML" in str(exc)
    else:
        raise AssertionError("wrong-protocol definition was not rejected")


def test_mcp_generation_swap_rejects_builtin_alias_collision_without_mutation() -> None:
    catalog = _native_catalog()
    before = catalog.names()
    generation = catalog.generation
    definition = _native_definition("Remote", "Remote tool.", category="mcp")

    with pytest.raises(ValueError, match="namespace conflict"):
        catalog.replace_mcp(((ExecutableToolBinding(definition, Capability("Remote")), ("Read",)),))

    assert catalog.names() == before
    assert catalog.generation == generation


def test_mcp_generation_swap_rejects_candidate_alias_collision_without_mutation() -> None:
    catalog = _native_catalog()
    generation = catalog.generation
    first = ExecutableToolBinding(
        _native_definition("First", "First remote.", category="mcp"),
        Capability("First"),
    )
    second = ExecutableToolBinding(
        _native_definition("Second", "Second remote.", category="mcp"),
        Capability("Second"),
    )

    with pytest.raises(ValueError, match="namespace conflict"):
        catalog.replace_mcp(((first, ("remote",)), (second, ("remote",))))

    assert catalog.generation == generation
    assert catalog.mcp_names() == []


def test_mcp_generation_swap_publishes_only_complete_snapshots() -> None:
    catalog = NativeToolCatalog()
    old = ExecutableToolBinding(_native_definition("Old", "Old remote.", category="mcp"), Capability("Old"))
    catalog.replace_mcp(((old, ("old:a", "old:b")),))
    new = ExecutableToolBinding(_native_definition("New", "New remote.", category="mcp"), Capability("New"))
    observed: set[frozenset[str]] = set()
    ready = threading.Event()

    def read_catalog() -> None:
        ready.set()
        for _ in range(10_000):
            observed.add(frozenset(catalog.mcp_names()))

    reader = threading.Thread(target=read_catalog)
    reader.start()
    ready.wait()
    before = catalog.generation
    catalog.replace_mcp(((new, ("new:a", "new:b")),))
    reader.join()

    assert observed <= {frozenset({"old:a", "old:b"}), frozenset({"new:a", "new:b"})}
    assert catalog.generation == before + 1
