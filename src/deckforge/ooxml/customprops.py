"""Пользовательские свойства документа (`docProps/custom.xml`) — запись и чтение.

Task 15 (ТЗ п.2.4 «версионирование скиллов и агентов», п.4 «промпты/конфиги
отдельными файлами»): по готовому `.pptx` должно быть видно, какими версиями
промптов и конфигов он собран — `deckforge.workflow.versions.manifest()`
даёт строку, `write_custom_property` кладёт её в стандартную OPC-часть
пользовательских свойств документа (та же часть, что Word/PowerPoint
показывают в диалоге «Свойства файла» → «Настраиваемые»).

python-pptx лениво создаёт `docProps/core.xml` при первом обращении к
`Presentation.core_properties`, но `docProps/custom.xml` не поддерживает
вовсе (нет части ни на чтение, ни на запись) — шаблоны в этом проекте
(экспорт из Google Slides) вообще приходят без `docProps/`, так что при
записи part, content-type override и relationship нужно завести с нуля,
а не только дописать существующие.
"""
from __future__ import annotations
import zipfile
from pathlib import Path

from lxml import etree

CUSTOM_PART_NAME = "docProps/custom.xml"
_CONTENT_TYPES_PART = "[Content_Types].xml"
_ROOT_RELS_PART = "_rels/.rels"

_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_CUSTOM_PROPERTIES_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
)
_CUSTOM_PROPERTIES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.custom-properties+xml"
)
# {D5CDD505-2E9C-101B-9397-08002B2CF9AE} — стандартный fmtid секции "Summary"
# пользовательских свойств, тот же, что пишут Word/PowerPoint/Excel.
_SUMMARY_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"


def read_custom_properties(pptx_path: Path) -> dict[str, str]:
    """Пользовательские свойства документа: имя -> текстовое значение.

    Пустой словарь, если у файла вовсе нет части `docProps/custom.xml`
    (обычный .pptx без ранее записанных свойств)."""
    with zipfile.ZipFile(pptx_path, "r") as zf:
        if CUSTOM_PART_NAME not in zf.namelist():
            return {}
        data = zf.read(CUSTOM_PART_NAME)
    root = etree.fromstring(data)
    result: dict[str, str] = {}
    for prop in root.findall(f"{{{_CP_NS}}}property"):
        name = prop.get("name")
        if name is None:
            continue
        child = prop[0] if len(prop) else None
        result[name] = child.text or "" if child is not None else ""
    return result


def write_custom_property(pptx_path: Path, name: str, value: str) -> None:
    """Записать/перезаписать одно пользовательское свойство документа.

    Переписывает .pptx целиком во временный файл и атомарно подменяет
    оригинал (`zipfile` не даёт редактировать архив на месте) — остальные
    части пакета копируются побайтово, не парсятся."""
    pptx_path = Path(pptx_path)
    with zipfile.ZipFile(pptx_path, "r") as zin:
        items: dict[str, bytes] = {info.filename: zin.read(info.filename) for info in zin.infolist()}

    items[_CONTENT_TYPES_PART] = _with_custom_properties_override(items[_CONTENT_TYPES_PART])
    items[_ROOT_RELS_PART] = _with_custom_properties_relationship(items[_ROOT_RELS_PART])
    items[CUSTOM_PART_NAME] = _with_property(items.get(CUSTOM_PART_NAME), name, value)

    tmp_path = pptx_path.with_suffix(pptx_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for part_name, data in items.items():
            zout.writestr(part_name, data)
    tmp_path.replace(pptx_path)


def _with_custom_properties_override(content_types_xml: bytes) -> bytes:
    root = etree.fromstring(content_types_xml)
    already = any(
        el.get("PartName") == f"/{CUSTOM_PART_NAME}"
        for el in root.findall(f"{{{_CT_NS}}}Override")
    )
    if not already:
        override = etree.SubElement(root, f"{{{_CT_NS}}}Override")
        override.set("PartName", f"/{CUSTOM_PART_NAME}")
        override.set("ContentType", _CUSTOM_PROPERTIES_CONTENT_TYPE)
    return _serialize(root)


def _with_custom_properties_relationship(root_rels_xml: bytes) -> bytes:
    root = etree.fromstring(root_rels_xml)
    rel_elements = root.findall(f"{{{_REL_NS}}}Relationship")
    if any(el.get("Type") == _CUSTOM_PROPERTIES_REL_TYPE for el in rel_elements):
        return _serialize(root)
    existing_ids = {el.get("Id") for el in rel_elements}
    rid = _next_rid(existing_ids)
    rel = etree.SubElement(root, f"{{{_REL_NS}}}Relationship")
    rel.set("Id", rid)
    rel.set("Type", _CUSTOM_PROPERTIES_REL_TYPE)
    rel.set("Target", CUSTOM_PART_NAME)
    return _serialize(root)


def _with_property(custom_xml: bytes | None, name: str, value: str) -> bytes:
    if custom_xml is None:
        root = etree.Element(f"{{{_CP_NS}}}Properties", nsmap={None: _CP_NS, "vt": _VT_NS})
    else:
        root = etree.fromstring(custom_xml)

    existing = [p for p in root.findall(f"{{{_CP_NS}}}property") if p.get("name") == name]
    if existing:
        prop = existing[0]
        for child in list(prop):
            prop.remove(child)
    else:
        prop = etree.SubElement(root, f"{{{_CP_NS}}}property")
        prop.set("fmtid", _SUMMARY_FMTID)
        prop.set("pid", str(_next_pid(root)))
        prop.set("name", name)

    lpwstr = etree.SubElement(prop, f"{{{_VT_NS}}}lpwstr")
    lpwstr.text = value
    return _serialize(root)


def _next_pid(properties_root) -> int:
    """pid 0 и 1 зарезервированы форматом (Dictionary/CodePage), первое
    пользовательское свойство начинается с 2."""
    used = {int(p.get("pid")) for p in properties_root.findall(f"{{{_CP_NS}}}property") if p.get("pid")}
    pid = 2
    while pid in used:
        pid += 1
    return pid


def _next_rid(existing_ids: set[str | None]) -> str:
    n = 1
    while f"rId{n}" in existing_ids:
        n += 1
    return f"rId{n}"


def _serialize(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
