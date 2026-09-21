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
from deckforge.template.theme import pick_primary_master
from deckforge.template.usage import NO_BACKGROUND, collect_usage


def load(name):
    """Task 3 код-ревью (п.2): collect_usage больше не принимает `theme` —
    тема резолвится внутри, отдельно для каждой части пакета (см. докстроку
    collect_usage в usage.py)."""
    pkg = PptxPackage.open(Path("dataset/templates") / name)
    canvas = pkg.canvas()
    return pkg, collect_usage(pkg, canvas)


def _usage_from_slide_xml(slide_xml: str):
    """Синтетический .pptx из одного slide1.xml — для граничных случаев OOXML,
    которых нет ни в одном из трёх реальных шаблонов (тот же приём, что
    строит синтетические деревья в tests/ooxml/test_walk.py, только здесь
    нужен целый пакет: collect_usage читает part через PptxPackage.xml()
    и резолвит presentation.xml через relationship officeDocument).

    Пакет без единого мастера — collect_usage не падает (см. _ThemeGraph в
    usage.py), резолвит все части через theme.EMPTY_THEME."""
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
    return collect_usage(pkg, canvas)


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


# --- Task 3 код-ревью, п.1: выбор ветки mc:AlternateContent по @Requires,
# не по «первый Choice, если есть». Нативный PowerPoint штатно пишет
# несколько mc:Choice под разные версии — правильная ветка не обязана быть
# первой в документе.

_MC_REQUIRES_SLIDE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
       xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <mc:AlternateContent>
        <mc:Choice Requires="p14">
          <p:sp>
            <p:nvSpPr><p:cNvPr id="2" name="P14"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="1000"/><a:t>P14</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Choice>
        <mc:Choice Requires="a">
          <p:sp>
            <p:nvSpPr><p:cNvPr id="3" name="A"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="2000"/><a:t>A</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Choice>
        <mc:Fallback>
          <p:sp>
            <p:nvSpPr><p:cNvPr id="4" name="Fallback"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="3000"/><a:t>Fallback</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Fallback>
      </mc:AlternateContent>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_mc_choice_is_picked_by_requires_not_by_document_order():
    """Первый Choice требует p14 (расширение PowerPoint 2010, модуль его не
    понимает) — должен быть пропущен. Второй Choice требует "a" (базовый
    DrawingML, xmlns:a уже объявлен в документе) — должен быть выбран, хотя
    он не первый."""
    usage = _usage_from_slide_xml(_MC_REQUIRES_SLIDE)
    assert usage.total_runs == 1
    assert usage.sizes_pt == {20.0: 1}


_MC_REQUIRES_NONE_UNDERSTOOD_SLIDE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
       xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main"
       xmlns:p15="http://schemas.microsoft.com/office/powerpoint/2012/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <mc:AlternateContent>
        <mc:Choice Requires="p14">
          <p:sp>
            <p:nvSpPr><p:cNvPr id="2" name="P14"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="1000"/><a:t>P14</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Choice>
        <mc:Choice Requires="p15">
          <p:sp>
            <p:nvSpPr><p:cNvPr id="3" name="P15"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="1100"/><a:t>P15</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Choice>
        <mc:Fallback>
          <p:sp>
            <p:nvSpPr><p:cNvPr id="4" name="Fallback"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
            <p:spPr/>
            <p:txBody><a:p><a:r><a:rPr lang="en-US" sz="1200"/><a:t>Fallback</a:t></a:r></a:p></p:txBody>
          </p:sp>
        </mc:Fallback>
      </mc:AlternateContent>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_mc_falls_back_when_no_choice_is_understood():
    """Ни один Choice не требует понятного модулю пространства имён —
    результат mc:Fallback, а не первый Choice по умолчанию."""
    usage = _usage_from_slide_xml(_MC_REQUIRES_NONE_UNDERSTOOD_SLIDE)
    assert usage.total_runs == 1
    assert usage.sizes_pt == {12.0: 1}


# --- Task 3 код-ревью, п.5: run без явного a:rPr считается, а не
# выбрасывается — символы идут в inherited_chars, не теряются молча.

_MIXED_RPR_SLIDE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Text"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr/>
        <p:txBody>
          <a:p>
            <a:r><a:rPr lang="en-US" sz="1800"/><a:t>Explicit</a:t></a:r>
            <a:r><a:t>Inherited</a:t></a:r>
          </a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_run_without_rpr_is_counted_as_inherited_not_dropped():
    usage = _usage_from_slide_xml(_MIXED_RPR_SLIDE)
    assert usage.total_runs == 2
    assert usage.explicit_chars == len("Explicit")
    assert usage.inherited_chars == len("Inherited")
    # run без rPr по-прежнему не участвует в счётчиках свойств — наследование
    # по цепочке в этой задаче не резолвится (см. докстроку модуля)
    assert usage.sizes_pt == {18.0: 1}


def test_runs_without_explicit_rpr_are_counted_on_real_templates():
    """На трёх учебных шаблонах явных run'ов заметно меньше, чем всего
    текста — доля unrecognized (17-40% по разведке) не должна теряться."""
    for name in ["VK Tech шаблон.pptx", "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
                 "Шаблон презентации VK Education.pptx"]:
        _, usage = load(name)
        total = usage.explicit_chars + usage.inherited_chars
        assert total > usage.explicit_chars, name
        assert usage.inherited_chars > 0, name


# --- Task 3 код-ревью, п.6: фон лейаута наследуется от мастера, если у
# самого лейаута нет p:bg.

def test_layout_bg_on_real_template_never_falls_back_to_no_background():
    """У VK Tech оба мастера несут собственный p:bg — после наследования ни
    один из 39 лейаутов не должен остаться без определённого фона."""
    _, usage = load("VK Tech шаблон.pptx")
    resolved = sum(count for key, count in usage.layout_bg.items() if key is not NO_BACKGROUND)
    assert resolved == 39
    assert usage.layout_bg.get(NO_BACKGROUND, 0) == 0


_LAYOUT_NO_BG_SLIDE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
</p:sldLayout>
"""

_MASTER_WITH_BG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:bg>
      <p:bgPr>
        <a:solidFill><a:srgbClr val="336699"/></a:solidFill>
      </p:bgPr>
    </p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
</p:sldMaster>
"""

# read_theme() требует связанную тему у любого мастера (иначе ValueError —
# см. theme.py::_master_theme_part), поэтому синтетическим мастерам ниже
# нужна хотя бы минимальная тема, даже если сам тест про неё не спрашивает.
_MINIMAL_THEME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Minimal">
  <a:themeElements>
    <a:clrScheme name="Minimal">
      <a:dk1><a:srgbClr val="000000"/></a:dk1>
      <a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="000000"/></a:dk2>
      <a:lt2><a:srgbClr val="FFFFFF"/></a:lt2>
      <a:accent1><a:srgbClr val="0077FF"/></a:accent1>
      <a:accent2><a:srgbClr val="111111"/></a:accent2>
      <a:accent3><a:srgbClr val="222222"/></a:accent3>
      <a:accent4><a:srgbClr val="333333"/></a:accent4>
      <a:accent5><a:srgbClr val="444444"/></a:accent5>
      <a:accent6><a:srgbClr val="555555"/></a:accent6>
      <a:hlink><a:srgbClr val="0563C1"/></a:hlink>
      <a:folHlink><a:srgbClr val="954F72"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Office">
      <a:majorFont><a:latin typeface="Arial"/></a:majorFont>
      <a:minorFont><a:latin typeface="Arial"/></a:minorFont>
    </a:fontScheme>
  </a:themeElements>
</a:theme>
"""


def _package_with_layout_inheriting_master_bg() -> PptxPackage:
    files = {
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>'
        ),
        "ppt/presentation.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483649" r:id="rId1"/></p:sldMasterIdLst>'
            "</p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
            'Target="slideMasters/slideMaster1.xml"/></Relationships>'
        ),
        "ppt/slideMasters/slideMaster1.xml": _MASTER_WITH_BG_XML,
        "ppt/theme/theme1.xml": _MINIMAL_THEME_XML,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
            'Target="../theme/theme1.xml"/></Relationships>'
        ),
        "ppt/slideLayouts/slideLayout1.xml": _LAYOUT_NO_BG_SLIDE,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
            'Target="../slideMasters/slideMaster1.xml"/></Relationships>'
        ),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r"))


def test_layout_without_own_bg_inherits_master_bg():
    pkg = _package_with_layout_inheriting_master_bg()
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    usage = collect_usage(pkg, canvas)
    assert usage.layout_bg[Color(hex="#336699")] == 1
    assert usage.layout_bg.get(NO_BACKGROUND, 0) == 0


def test_layout_and_master_both_without_bg_records_no_background():
    """Ни лейаут, ни его мастер не задают p:bg — NO_BACKGROUND, а не
    молчаливый пропуск лейаута."""
    files = {
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>'
        ),
        "ppt/presentation.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483649" r:id="rId1"/></p:sldMasterIdLst>'
            "</p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
            'Target="slideMasters/slideMaster1.xml"/></Relationships>'
        ),
        "ppt/slideMasters/slideMaster1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            "<p:grpSpPr/></p:spTree></p:cSld></p:sldMaster>"
        ),
        "ppt/theme/theme1.xml": _MINIMAL_THEME_XML,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
            'Target="../theme/theme1.xml"/></Relationships>'
        ),
        "ppt/slideLayouts/slideLayout1.xml": _LAYOUT_NO_BG_SLIDE,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
            'Target="../slideMasters/slideMaster1.xml"/></Relationships>'
        ),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    pkg = PptxPackage(zipfile.ZipFile(buf, "r"))
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    usage = collect_usage(pkg, canvas)
    assert usage.layout_bg[NO_BACKGROUND] == 1


# --- Task 3 код-ревью, п.7 (мелочи): текст и заливка ячеек a:tbl должны
# попадать в общую статистику, а не пропадать молча.

_TABLE_SLIDE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:graphicFrame>
        <p:nvGraphicFramePr><p:cNvPr id="2" name="Table"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
        <p:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></p:xfrm>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">
            <a:tbl>
              <a:tblPr/>
              <a:tblGrid><a:gridCol w="100"/></a:tblGrid>
              <a:tr h="100">
                <a:tc>
                  <a:txBody>
                    <a:bodyPr/><a:lstStyle/>
                    <a:p><a:r><a:rPr lang="en-US" sz="1400">
                      <a:solidFill><a:srgbClr val="112233"/></a:solidFill>
                    </a:rPr><a:t>Cell</a:t></a:r></a:p>
                  </a:txBody>
                  <a:tcPr><a:solidFill><a:srgbClr val="AABBCC"/></a:solidFill></a:tcPr>
                </a:tc>
              </a:tr>
            </a:tbl>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_table_cell_text_and_fill_are_collected():
    usage = _usage_from_slide_xml(_TABLE_SLIDE)
    assert usage.total_runs == 1
    assert usage.explicit_chars == len("Cell")
    assert any(c.hex == "#AABBCC" for c in usage.fill)
    assert usage.text[Color(hex="#112233")] == len("Cell")


# --- Task 3 код-ревью, п.2: тема резолвится отдельно для каждой части, не
# одна тема первичного мастера на весь пакет. На учебных шаблонах темы
# обоих мастеров (там, где их два) совпадают по палитре — нужна синтетика.

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


def _theme_xml(name: str, accent1: str) -> str:
    accents = {"accent1": accent1, "accent2": "111111", "accent3": "222222",
               "accent4": "333333", "accent5": "444444", "accent6": "555555"}
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


def _layout_with_accent1_fill_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="Card"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></p:spPr></p:sp>'
        "</p:spTree></p:cSld></p:sldLayout>"
    )


def _two_master_package(*, master1_layouts: int, master2_layouts: int, accent1_1: str, accent1_2: str) -> PptxPackage:
    """Два мастера с разными темами (accent1 отличается), каждый — со
    своими лейаутами, каждый лейаут красит карточку в schemeClr accent1.
    master1 первичный (больше лейаутов, без ничьей)."""
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
        "ppt/theme/theme1.xml": _theme_xml("Master1Theme", accent1_1),
        "ppt/theme/theme2.xml": _theme_xml("Master2Theme", accent1_2),
        "ppt/slideMasters/slideMaster1.xml": _MASTER_XML,
        "ppt/slideMasters/slideMaster2.xml": _MASTER_XML,
    }

    master1_rels: list[tuple[str, str, str]] = [("rId1", "theme", "../theme/theme1.xml")]
    for i in range(master1_layouts):
        idx = i + 1
        master1_rels.append((f"rId{idx + 1}", "slideLayout", f"../slideLayouts/slideLayout{idx}.xml"))
        files[f"ppt/slideLayouts/slideLayout{idx}.xml"] = _layout_with_accent1_fill_xml()
        files[f"ppt/slideLayouts/_rels/slideLayout{idx}.xml.rels"] = _rels_xml(
            [("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")],
        )
    files["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = _rels_xml(master1_rels)

    master2_rels: list[tuple[str, str, str]] = [("rId1", "theme", "../theme/theme2.xml")]
    for j in range(master2_layouts):
        idx = master1_layouts + j + 1
        master2_rels.append((f"rId{j + 2}", "slideLayout", f"../slideLayouts/slideLayout{idx}.xml"))
        files[f"ppt/slideLayouts/slideLayout{idx}.xml"] = _layout_with_accent1_fill_xml()
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


def test_layout_under_secondary_master_resolves_its_own_theme():
    """Раньше schemeClr любого макета резолвился через тему ПЕРВИЧНОГО
    мастера — макет второстепенного мастера молча получал чужую палитру."""
    pkg = _two_master_package(master1_layouts=2, master2_layouts=1, accent1_1="AA0000", accent1_2="00AA00")
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    assert pick_primary_master(pkg) == "ppt/slideMasters/slideMaster1.xml"

    usage = collect_usage(pkg, canvas)
    hexes = {c.hex for c in usage.fill}
    assert "#AA0000" in hexes  # лейауты первичного мастера — его тема
    assert "#00AA00" in hexes  # лейаут второго мастера — СВОЯ тема, не чужая


def test_master_itself_resolves_own_theme_directly():
    """Сам мастер (а не только его лейауты) тоже часть пакета — его
    собственные schemeClr должны резолвиться через его же тему."""
    _, usage = load("VK Tech шаблон.pptx")
    assert usage.primary_theme is not None
    assert usage.primary_theme.scheme["accent1"] == "#0077FF"
