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

@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem[:20])
def test_media_inventory_is_not_empty(path):
    with PptxPackage.open(path) as pkg:
        media = pkg.media()
        assert media
        assert all(m.size_bytes > 0 for m in media)
