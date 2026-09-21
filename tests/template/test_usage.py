"""Фактическая палитра и шрифты по всем слайдам/лейаутам/мастерам шаблона.

Тесты — дословно из брифа Task 3 (Step 3), плюс несколько дополнительных
на конкретные числа/эвристики из разведки (bg dk1 у лейаутов, noFill-счётчик,
взвешивание текста по символам), которых бриф явно требует в тексте задачи,
но не дал в виде кода.
"""
import io
import zipfile

import pytest
from pathlib import Path
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.color import Color
from deckforge.ooxml.geometry import Canvas
from deckforge.template.theme import ThemeInfo, read_theme, pick_primary_master
from deckforge.template.usage import collect_usage


def load(name):
    pkg = PptxPackage.open(Path("dataset/templates") / name)
    theme = read_theme(pkg, pick_primary_master(pkg))
    canvas = pkg.canvas()
    return pkg, collect_usage(pkg, canvas, theme)


_EMPTY_THEME = ThemeInfo(
    scheme={}, clr_map={}, major_font="", minor_font="", scheme_name="",
    font_scheme_degraded=True, text_styles_degraded=True, is_stock_office_palette=False,
)


def _usage_from_slide_xml(slide_xml: str):
    """Синтетический .pptx из одного slide1.xml — для граничных случаев OOXML,
    которых нет ни в одном из трёх реальных шаблонов (тот же приём, что
    строит синтетические деревья в tests/ooxml/test_walk.py, только здесь
    нужен целый пакет: collect_usage читает part через PptxPackage.xml()
    и резолвит presentation.xml через relationship officeDocument)."""
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
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    buf.seek(0)
    pkg = PptxPackage(zipfile.ZipFile(buf, "r"))
    canvas = Canvas(width_emu=12192000, height_emu=6858000)  # norm=1, кегли не масштабируются
    return collect_usage(pkg, canvas, _EMPTY_THEME)


def test_workspace_usage_catches_colors_absent_from_theme():
    """У WorkSpace srgbClr на слайдах вдвое больше, чем schemeClr:
    палитра только из темы потеряет половину реальных цветов."""
    _, usage = load("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    hexes = {c.hex for c in usage.fill} | {c.hex for c in usage.text}
    assert "#212121" in hexes
    assert "#6DBCFF" in hexes


def test_alpha_fills_are_preserved():
    _, usage = load("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    assert any(c.hex == "#0077FF" and c.alpha < 0.5 for c in usage.fill)


def test_play_is_the_dominant_font_everywhere():
    for name in ["VK Tech шаблон.pptx", "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
                 "Шаблон презентации VK Education.pptx"]:
        _, usage = load(name)
        top = max(usage.fonts.values(), key=lambda f: f.chars)
        assert top.family == "Play", name


def test_embedded_fonts_are_reported():
    _, usage = load("VK Tech шаблон.pptx")
    assert usage.fonts["Play"].embedded is True
    assert usage.fonts["Play"].bold_available is True
    assert usage.fonts["Play"].italic_available is False


def test_bold_is_not_idiomatic_in_these_templates():
    """bold — 4 run'а из 744 у VK Tech. Генератор, ставящий bold «как обычно»,
    будет вылезать из шаблона."""
    _, usage = load("VK Tech шаблон.pptx")
    assert usage.bold_runs / usage.total_runs < 0.05
    assert usage.italic_runs == 0


def test_sizes_are_normalised_to_reference_canvas():
    """VK Tech свёрстан на холсте 10″; 24pt там — это 32pt в нормированной шкале."""
    _, usage = load("VK Tech шаблон.pptx")
    assert max(usage.sizes_pt) > 40


# --- дополнительные тесты на требования, явно сформулированные текстом
# брифа (не только кодом Step 3) ---

def test_text_colors_are_weighted_by_characters_not_runs():
    """Заголовок из девяти знаков не должен весить столько же, сколько
    абзац из сорока — вес в Counter должен расти вместе с длиной текста."""
    _, usage = load("VK Tech шаблон.pptx")
    total_chars_weighted = sum(usage.text.values())
    total_runs_with_text = usage.total_runs
    # если бы вес был по run'ам, сумма весов не могла бы превышать число run'ов
    # больше, чем на пару штук; при взвешивании по символам она обязана быть
    # заметно больше (абзацы в десятки символов на run) — иначе взвешивание
    # по факту не работает и тест на него бессмыслен.
    assert total_chars_weighted > total_runs_with_text


def test_no_fill_shapes_are_counted_separately():
    """У VK Tech масса невидимых контейнеров-карточек (noFill) — это сигнал
    о вёрстке, а не мусор для отбрасывания."""
    _, usage = load("VK Tech шаблон.pptx")
    assert usage.no_fill_shapes > 0


def test_unresolved_colors_are_collected_not_dropped():
    """Ни один из трёх шаблонов не должен ронять сборщик на нераспознанном
    цвете — список должен существовать и быть списком UnresolvedColor."""
    for name in ["VK Tech шаблон.pptx", "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
                 "Шаблон презентации VK Education.pptx"]:
        _, usage = load(name)
        assert isinstance(usage.unresolved, list)


def test_fill_and_text_counters_hold_color_instances():
    _, usage = load("VK Tech шаблон.pptx")
    assert all(isinstance(c, Color) for c in usage.fill)
    assert all(isinstance(c, Color) for c in usage.text)


# --- граничные случаи OOXML, которых нет ни в одном из трёх реальных
# шаблонов (все три — Google-экспорт), но которые встречаются в нативном
# PowerPoint и потому вероятны на незнакомом шаблоне с защиты. Найдены
# ревью на код (adversarial-reviewer), подтверждены синтетическим XML —
# без ЛЦТ2026, который в тестах участвовать не должен.

_ALTERNATE_CONTENT_SLIDE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <mc:AlternateContent>
        <mc:Choice Requires="v">
          <p:sp>
            <p:nvSpPr><p:cNvPr id="2" name="Choice"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="1800"/><a:t>Alt</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Choice>
        <mc:Fallback>
          <p:sp>
            <p:nvSpPr><p:cNvPr id="3" name="Fallback"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="1800"/><a:t>Alt</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Fallback>
      </mc:AlternateContent>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="4" name="Control"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="1200"/><a:t>Control</a:t></a:r></a:p></p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_mc_alternate_content_branches_are_not_double_counted():
    """mc:Choice и mc:Fallback — взаимоисключающие альтернативы одного и того
    же шейпа (Markup Compatibility, ECMA-376 Part 3), а не два разных шейпа.
    Наивный обход по локальному имени тега (lxml .iter()) спускается в обе
    ветки и удваивает всё, что связано с этим шейпом."""
    usage = _usage_from_slide_xml(_ALTERNATE_CONTENT_SLIDE)
    assert usage.total_runs == 2  # одна ветка AlternateContent + контрольный шейп
    assert usage.sizes_pt[18.0] == 1
    assert usage.sizes_pt[12.0] == 1


_BLIP_FILL_SLIDE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Photo card"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:blipFill><a:blip r:embed="rId1"/></a:blipFill></p:spPr>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_blip_fill_is_not_reported_as_unresolved_color():
    """Заливка картинкой — не «нераспознанный цвет» (нечего распознавать),
    тот же случай по смыслу, что noFill/gradFill: не единый цвет, но и не
    ошибка разбора. Подтверждено на контрольном шаблоне защиты (в тестах
    сам файл не участвует, см. правило исключения ЛЦТ2026 выше)."""
    usage = _usage_from_slide_xml(_BLIP_FILL_SLIDE)
    assert usage.unresolved == []
    assert usage.no_fill_shapes == 0
    assert len(usage.fill) == 0
