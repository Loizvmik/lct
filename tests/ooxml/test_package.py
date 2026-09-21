import io
import zipfile

import pytest
from pathlib import Path
from deckforge.ooxml.package import PptxPackage

# ЛЦТ2026 держится контрольным шаблоном для защиты — в тестах не участвует,
# поэтому из дословного glob() брифа его явно исключаем.
TEMPLATES = sorted(
    p for p in Path("dataset/templates").glob("*.pptx")
    if not p.stem.startswith("ЛЦТ2026")
)

@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem[:20])
def test_opens_and_lists_masters(path):
    with PptxPackage.open(path) as pkg:
        masters = [n for n in pkg.names() if n.startswith("ppt/slideMasters/slideMaster")]
        assert masters, f"{path.name}: не найдено ни одного мастера"

def test_theme_is_resolved_through_rels_not_by_filename():
    """У VK WorkSpace theme1.xml — офисная заглушка, брендовая тема в theme2."""
    path = Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    with PptxPackage.open(path) as pkg:
        themes = pkg.related("ppt/slideMasters/slideMaster1.xml", "theme")
        assert themes == ["ppt/theme/theme2.xml"]


def test_related_caches_like_rels(monkeypatch):
    """related() должен кэшировать результат так же, как rels() — не
    перечитывать и не перепарсивать .rels-XML из zip на каждый вызов."""
    path = Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    with PptxPackage.open(path) as pkg:
        reads: list[str] = []
        orig_part = pkg.part

        def spy_part(name: str) -> bytes:
            reads.append(name)
            return orig_part(name)

        monkeypatch.setattr(pkg, "part", spy_part)

        pkg.related("ppt/slideMasters/slideMaster1.xml", "theme")
        pkg.related("ppt/slideMasters/slideMaster1.xml", "theme")
        pkg.related("ppt/slideMasters/slideMaster1.xml", "slideLayout")

        rels_reads = [n for n in reads if n.endswith(".rels")]
        assert rels_reads == ["ppt/slideMasters/_rels/slideMaster1.xml.rels"]

@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem[:20])
def test_media_inventory_is_not_empty(path):
    with PptxPackage.open(path) as pkg:
        media = pkg.media()
        assert media
        assert all(m.size_bytes > 0 for m in media)


def test_canvas_reads_slide_size_from_presentation_xml():
    """VK Tech свёрстан на холсте 10×5.625″ (9144000×5143500 EMU) — не на
    стандартном 13.333″, как остальные. Task 3 нормирует кегли на canvas.norm,
    для этого нужен настоящий размер, а не догадка по большинству шаблонов."""
    path = Path("dataset/templates/VK Tech шаблон.pptx")
    with PptxPackage.open(path) as pkg:
        canvas = pkg.canvas()
        assert canvas.width_emu == 9144000
        assert canvas.height_emu == 5143500


def test_canvas_is_standard_for_workspace_and_education():
    for name in [
        "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
        "Шаблон презентации VK Education.pptx",
    ]:
        with PptxPackage.open(Path("dataset/templates") / name) as pkg:
            canvas = pkg.canvas()
            assert canvas.width_emu == 12192000
            assert canvas.height_emu == 6858000


def test_canvas_raises_value_error_when_sld_sz_missing():
    """Task 3 code review (finding 7): canvas() падал AttributeError на
    presentation.xml без p:sldSz. Соседние методы того же файла
    (presentation_part, _master_theme_part в theme.py) в похожей ситуации
    кидают понятный ValueError с именем части — canvas() должен вести себя
    так же, а не ронять непонятный AttributeError на None.find()."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>',
        )
        zf.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )
    buf.seek(0)
    pkg = PptxPackage(zipfile.ZipFile(buf, "r"))
    with pytest.raises(ValueError, match="ppt/presentation.xml"):
        pkg.canvas()
