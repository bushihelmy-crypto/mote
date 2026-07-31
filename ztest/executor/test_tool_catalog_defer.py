"""Protocol-specific deferred-tool projection tests."""

from __future__ import annotations

from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.tool_binding import BoundTool
from mote.runtime.tools.tool_catalog import NativeToolCatalog, XmlToolCatalog


class Capability:
    def __init__(self, name: str) -> None:
        self.name = name


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
        catalog.register(BoundTool(definition, Capability(name)), [name])
    return catalog


def _native_catalog(*, revealed: set[str] | None = None) -> NativeToolCatalog:
    current = revealed or set()
    catalog = NativeToolCatalog(deferred={"Convert"}, get_revealed=lambda: current)
    for name, description in (
        ("Read", "Read a file."),
        ("Convert", "Convert an image."),
    ):
        definition = _native_definition(name, description)
        catalog.register(BoundTool(definition, Capability(name)), [name])
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
    catalog.register(BoundTool(definition, Capability("Convert")), ["Convert"])

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
    xml.register(BoundTool(native_definition, Capability("Wrong")), ["Wrong"])
    try:
        xml.schemas_for(None)
    except TypeError as exc:
        assert "non-XML" in str(exc)
    else:
        raise AssertionError("wrong-protocol definition was not rejected")
