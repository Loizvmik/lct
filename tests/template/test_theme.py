"""Тема оформления через rels, детект заглушек Google-экспорта.

Тесты — дословно из брифа Task 3 (.superpowers/sdd/task-3-brief.md, Step 1),
с одним отклонением: TEMPLATES отфильтровывает ЛЦТ2026 — тот же приём и
та же причина, что в tests/ooxml/test_package.py и tests/ooxml/test_walk.py
(контрольный шаблон для защиты, в тестах не участвует), а не голый glob
брифа, который его случайно тоже захватывает.
"""
import pytest
from pathlib import Path
from deckforge.ooxml.package import PptxPackage
from deckforge.template.theme import read_theme, pick_primary_master

TEMPLATES = sorted(
    p for p in Path("dataset/templates").glob("*.pptx")
    if not p.stem.startswith("ЛЦТ2026")
)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem[:20])
def test_google_export_font_scheme_is_flagged_degraded(path):
    """Во всех трёх шаблонах fontScheme = Office/Arial. Парсер обязан это заметить,
    иначе построит типографику из заглушки."""
    with PptxPackage.open(path) as pkg:
        theme = read_theme(pkg, pick_primary_master(pkg))
        assert theme.font_scheme_degraded is True
        assert theme.text_styles_degraded is True


def test_workspace_primary_master_points_to_theme2():
    path = Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    with PptxPackage.open(path) as pkg:
        theme = read_theme(pkg, pick_primary_master(pkg))
        assert theme.scheme_name == "VK Tech 2024"
        assert theme.scheme["accent1"] == "#0077FF"
        assert theme.is_stock_office_palette is False


def test_stock_office_palette_is_detected():
    from deckforge.template.theme import is_stock_office
    assert is_stock_office({"accent1": "#4472C4", "accent2": "#ED7D31", "accent3": "#A5A5A5",
                            "accent4": "#FFC000", "accent5": "#5B9BD5", "accent6": "#70AD47"})
    assert not is_stock_office({"accent1": "#0077FF", "accent2": "#00E9FF", "accent3": "#AAFBFF",
                                "accent4": "#EDF3FC", "accent5": "#7C8A9A", "accent6": "#202020"})


@pytest.mark.parametrize("path,expected", [
    ("VK Tech шаблон.pptx", "#0077FF"),
    ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", "#0077FF"),
    ("Шаблон презентации VK Education.pptx", "#0077FF"),
])
def test_brand_accent_survives_extraction(path, expected):
    with PptxPackage.open(Path("dataset/templates") / path) as pkg:
        theme = read_theme(pkg, pick_primary_master(pkg))
        assert theme.scheme["accent1"] == expected


def test_vktech_primary_master_has_most_layouts():
    """VK Tech: master1 → theme1, 3 лейаута; master2 → theme2, 36 лейаутов.
    Обе темы брендовые (не стоковые) — выбор идёт по числу лейаутов."""
    path = Path("dataset/templates/VK Tech шаблон.pptx")
    with PptxPackage.open(path) as pkg:
        master = pick_primary_master(pkg)
        assert master == "ppt/slideMasters/slideMaster2.xml"


def test_education_primary_master_has_most_layouts():
    """Education: master1 → theme1, 26 лейаутов; master2 → theme3, 2 лейаута.
    Обе темы — «VK Education», выбор снова по числу лейаутов."""
    path = Path("dataset/templates/Шаблон презентации VK Education.pptx")
    with PptxPackage.open(path) as pkg:
        master = pick_primary_master(pkg)
        assert master == "ppt/slideMasters/slideMaster1.xml"


def test_orphan_stock_theme_is_never_picked():
    """У всех трёх шаблонов есть «сиротская» тема «Тема Office», ни к одному
    мастеру не привязанная напрямую через master.rels — pick_primary_master
    обязан вернуть мастер с реальной темой, а не наткнуться на сироту."""
    for name in ["VK Tech шаблон.pptx", "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
                 "Шаблон презентации VK Education.pptx"]:
        with PptxPackage.open(Path("dataset/templates") / name) as pkg:
            theme = read_theme(pkg, pick_primary_master(pkg))
            assert theme.is_stock_office_palette is False
