from mote.runtime.tools.provider import NativeToolset, XmlToolset


class Deps:
    pass


xml_tools: XmlToolset[Deps] = XmlToolset("xml", ())


def require_native(toolset: NativeToolset[Deps]) -> None:
    del toolset


require_native(xml_tools)
