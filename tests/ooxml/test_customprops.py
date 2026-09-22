"""`docProps/custom.xml` — запись и чтение пользовательских свойств (Task 15).

Шаблоны датасета (экспорт из Google Slides) приходят вовсе без `docProps/`
(см. `test_write_creates_docprops_from_scratch`) — самый частый в проекте
случай, не "дописать существующую часть", а "завести с нуля"."""
from __future__ import annotations
import shutil
import zipfile
from pathlib import Path

from deckforge.ooxml.customprops import read_custom_properties, write_custom_property

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")


def _copy(tmp_path: Path) -> Path:
    dst = tmp_path / "deck.pptx"
    shutil.copy2(TEMPLATE, dst)
    return dst


def test_source_template_has_no_docprops_yet():
    """Заявление докстрока о шаблонах Google Slides — а не догадка."""
    with zipfile.ZipFile(TEMPLATE) as zf:
        assert not [n for n in zf.namelist() if n.startswith("docProps/")]


def test_read_returns_empty_dict_when_part_is_absent(tmp_path):
    assert read_custom_properties(_copy(tmp_path)) == {}


def test_write_creates_docprops_from_scratch(tmp_path):
    path = _copy(tmp_path)
    write_custom_property(path, "deckforge_workflow", "outline-writer 1.0.0")
    assert read_custom_properties(path) == {"deckforge_workflow": "outline-writer 1.0.0"}


def test_write_is_valid_zip_and_pptx_stays_openable(tmp_path):
    path = _copy(tmp_path)
    write_custom_property(path, "deckforge_workflow", "outline-writer 1.0.0")
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        assert "ppt/presentation.xml" in zf.namelist()


def test_writing_twice_overwrites_same_property_not_duplicates(tmp_path):
    path = _copy(tmp_path)
    write_custom_property(path, "deckforge_workflow", "outline-writer 1.0.0")
    write_custom_property(path, "deckforge_workflow", "outline-writer 1.1.0")
    props = read_custom_properties(path)
    assert props == {"deckforge_workflow": "outline-writer 1.1.0"}


def test_writing_a_second_property_keeps_the_first(tmp_path):
    path = _copy(tmp_path)
    write_custom_property(path, "deckforge_workflow", "outline-writer 1.0.0")
    write_custom_property(path, "deckforge_build", "2026-09-22")
    props = read_custom_properties(path)
    assert props == {"deckforge_workflow": "outline-writer 1.0.0", "deckforge_build": "2026-09-22"}


def test_write_adds_content_type_override_and_relationship_once(tmp_path):
    path = _copy(tmp_path)
    write_custom_property(path, "deckforge_workflow", "outline-writer 1.0.0")
    write_custom_property(path, "deckforge_workflow", "outline-writer 1.0.1")
    with zipfile.ZipFile(path) as zf:
        content_types = zf.read("[Content_Types].xml").decode("utf-8")
        rels = zf.read("_rels/.rels").decode("utf-8")
    assert content_types.count("docProps/custom.xml") == 1
    assert rels.count("custom-properties") == 1
