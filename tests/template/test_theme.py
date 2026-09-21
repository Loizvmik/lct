"""Тема оформления через rels, детект заглушек Google-экспорта.

Тесты — дословно из брифа Task 3 (.superpowers/sdd/task-3-brief.md, Step 1),
с одним отклонением: TEMPLATES отфильтровывает ЛЦТ2026 — тот же приём и
та же причина, что в tests/ooxml/test_package.py и tests/ooxml/test_walk.py
(контрольный шаблон для защиты, в тестах не участвует), а не голый glob
брифа, который его случайно тоже захватывает.
"""
import io
import zipfile

import pytest
from pathlib import Path
from deckforge.ooxml.package import PptxPackage
from deckforge.template.theme import (
    ThemeInfo,
    read_theme,
    pick_primary_master,
    refine_font_scheme_degraded,
    _STOCK_OFFICE_ACCENTS,
)

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


# --- синтетические пакеты с двумя мастерами (Task 3 код-ревью, п.2 и п.3):
# на всех трёх учебных шаблонах темы разных мастеров (там, где мастеров
# два — VK Tech, VK Education) совпадают по палитре — реальные файлы не
# различают правильное/неправильное поведение, нужна синтетика.

def _rels_xml(entries: list[tuple[str, str, str]]) -> str:
    rels = "".join(
        f'<Relationship Id="{rid}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{typ}" '
        f'Target="{target}"/>'
        for rid, typ, target in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def _theme_xml(name: str, accents: dict[str, str]) -> str:
    slots = {"dk1": "000000", "lt1": "FFFFFF", "dk2": "000000", "lt2": "FFFFFF",
              **accents, "hlink": "0563C1", "folHlink": "954F72"}
    clr_children = "".join(f'<a:{slot}><a:srgbClr val="{val}"/></a:{slot}>' for slot, val in slots.items())
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'name="{name}"><a:themeElements>'
        f'<a:clrScheme name="{name}">{clr_children}</a:clrScheme>'
        '<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Arial"/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Arial"/></a:minorFont></a:fontScheme>'
        "</a:themeElements></a:theme>"
    )


_MASTER_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    "<p:grpSpPr/></p:spTree></p:cSld></p:sldMaster>"
)

_LAYOUT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    "<p:grpSpPr/></p:spTree></p:cSld></p:sldLayout>"
)


def _two_master_package(
    *, master1_layouts: int, master2_layouts: int,
    master1_accents: dict[str, str], master2_accents: dict[str, str],
) -> PptxPackage:
    """Пакет с двумя мастерами (свои темы, свои лейауты), без слайдов —
    их эта задача не проверяет. Общий строитель для тестов на п.2 (палитра
    второстепенного мастера) и п.3 (отсев стокового мастера)."""
    files: dict[str, str] = {
        "_rels/.rels": _rels_xml([("rId1", "officeDocument", "ppt/presentation.xml")]),
        "ppt/presentation.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483649" r:id="rId1"/>'
            '<p:sldMasterId id="2147483650" r:id="rId2"/></p:sldMasterIdLst>'
            '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": _rels_xml([
            ("rId1", "slideMaster", "slideMasters/slideMaster1.xml"),
            ("rId2", "slideMaster", "slideMasters/slideMaster2.xml"),
        ]),
        "ppt/theme/theme1.xml": _theme_xml("Master1Theme", master1_accents),
        "ppt/theme/theme2.xml": _theme_xml("Master2Theme", master2_accents),
        "ppt/slideMasters/slideMaster1.xml": _MASTER_XML,
        "ppt/slideMasters/slideMaster2.xml": _MASTER_XML,
    }

    master1_rels: list[tuple[str, str, str]] = [("rId1", "theme", "../theme/theme1.xml")]
    for i in range(master1_layouts):
        idx = i + 1
        master1_rels.append((f"rId{idx + 1}", "slideLayout", f"../slideLayouts/slideLayout{idx}.xml"))
        files[f"ppt/slideLayouts/slideLayout{idx}.xml"] = _LAYOUT_XML
        files[f"ppt/slideLayouts/_rels/slideLayout{idx}.xml.rels"] = _rels_xml(
            [("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")],
        )
    files["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = _rels_xml(master1_rels)

    master2_rels: list[tuple[str, str, str]] = [("rId1", "theme", "../theme/theme2.xml")]
    for j in range(master2_layouts):
        idx = master1_layouts + j + 1
        master2_rels.append((f"rId{j + 2}", "slideLayout", f"../slideLayouts/slideLayout{idx}.xml"))
        files[f"ppt/slideLayouts/slideLayout{idx}.xml"] = _LAYOUT_XML
        files[f"ppt/slideLayouts/_rels/slideLayout{idx}.xml.rels"] = _rels_xml(
            [("rId1", "slideMaster", "../slideMasters/slideMaster2.xml")],
        )
    files["ppt/slideMasters/_rels/slideMaster2.xml.rels"] = _rels_xml(master2_rels)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r"))


def _brand_accents(accent1: str) -> dict[str, str]:
    return {"accent1": accent1, "accent2": "111111", "accent3": "222222",
            "accent4": "333333", "accent5": "444444", "accent6": "555555"}


def test_stock_master_is_rejected_even_with_more_layouts():
    """Task 3 код-ревью (п.3): отсев стоковой темы ни разу не сработал на
    учебных шаблонах — там стоковая тема всегда «сиротская», до этой ветки
    дело не доходит. Синтетика: стоковый мастер несёт БОЛЬШЕ лейаутов, чтобы
    тест проверял именно отсев по is_stock_office, а не победу по счёту."""
    pkg = _two_master_package(
        master1_layouts=5, master1_accents=dict(_STOCK_OFFICE_ACCENTS),
        master2_layouts=2, master2_accents=_brand_accents("0077FF"),
    )
    master = pick_primary_master(pkg)
    assert master == "ppt/slideMasters/slideMaster2.xml"
    theme = read_theme(pkg, master)
    assert theme.is_stock_office_palette is False
    assert theme.scheme["accent1"] == "#0077FF"


# --- refine_font_scheme_degraded (Task 3 код-ревью, п.4): синтетические
# случаи, не требующие пакета вовсе — чистая функция от ThemeInfo + счётчика
# символов по шрифтам.

def _degraded_office_theme(font: str) -> ThemeInfo:
    return ThemeInfo(
        scheme={}, clr_map={}, major_font=font, minor_font=font, scheme_name="",
        font_scheme_degraded=True, text_styles_degraded=False, is_stock_office_palette=False,
    )


def test_theme_font_matching_real_text_is_not_degraded():
    """Тема заявляет Arial, и текст в шаблоне тоже набран Arial (не Play,
    не что-то другое) — деградации нет, это осознанный выбор шрифта."""
    theme = _degraded_office_theme("Arial")
    refined = refine_font_scheme_degraded(theme, {"Arial": 500})
    assert refined.font_scheme_degraded is False


def test_theme_font_barely_present_in_text_stays_degraded():
    """Тема заявляет Arial, но большинство текста набрано другим шрифтом —
    у VK Education Arial встречается в четверти-трети текста, но не
    доминирует (доминирует Play): деградация обязана остаться True."""
    theme = _degraded_office_theme("Arial")
    refined = refine_font_scheme_degraded(theme, {"Arial": 300, "Play": 700})
    assert refined.font_scheme_degraded is True


def test_refine_does_not_raise_flag_that_was_not_set():
    """Нечего снимать: структурный сигнал уже False (тема не похожа на
    заглушку) — refine не должен внезапно поднять флаг."""
    theme = ThemeInfo(
        scheme={}, clr_map={}, major_font="Play", minor_font="Play", scheme_name="",
        font_scheme_degraded=False, text_styles_degraded=False, is_stock_office_palette=False,
    )
    refined = refine_font_scheme_degraded(theme, {"Play": 1000})
    assert refined.font_scheme_degraded is False
