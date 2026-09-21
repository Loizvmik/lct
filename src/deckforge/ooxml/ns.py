"""Пространства имён OOXML и мелкие xml-хелперы, общие для пакета ooxml."""
from __future__ import annotations
from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def qn(tag: str) -> str:
    """`"a:srgbClr"` → `"{http://.../drawingml/2006/main}srgbClr"`."""
    prefix, local = tag.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"


def local_name(el: etree._Element) -> str:
    """Имя тега без пространства имён: `{...}srgbClr` → `srgbClr`."""
    return etree.QName(el).localname
