"""Сетка шаблона (поля/колонки/якоря) — тесты дословно из брифа Task 4
(Step 3), .superpowers/sdd/task-4-brief.md.

Как и в test_typography.py, `profile_fixture` в телах тестов брифа нужен как
параметр — иначе pytest не подставит фикстуру и вызов упадёт с NameError;
это единственная правка против буквального текста брифа.
"""
import io
import zipfile

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.template.grid import build_grid, cluster


def test_margins_match_measured_values(profile_fixture):
    """Числа замерены разведкой; допуск 0.5 п.п."""
    cases = [("VK Tech шаблон.pptx", 0.0463), ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", 0.0351),
             ("Шаблон презентации VK Education.pptx", 0.0540)]
    for name, expected in cases:
        grid = profile_fixture(name).grid
        assert abs(grid.margin_left - expected) < 0.005, name


def test_education_margins_are_symmetric(profile_fixture):
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert abs(grid.margin_left - grid.margin_right) < 0.002


def test_education_has_exact_half_split(profile_fixture):
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert any(abs(axis - 0.5) < 0.003 for axis in grid.columns)


def test_title_anchor_is_found(profile_fixture):
    for name, expected in [("VK Tech шаблон.pptx", 0.055),
                           ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", 0.0619),
                           ("Шаблон презентации VK Education.pptx", 0.1009)]:
        assert abs(profile_fixture(name).grid.anchors["title_top"] - expected) < 0.008, name


def test_vertical_rhythm_is_reported_as_low_confidence(profile_fixture):
    """Глобального baseline grid в этих файлах нет. Парсер обязан сказать об этом,
    а не выдать шум за сетку."""
    grid = profile_fixture("VK Tech шаблон.pptx").grid
    assert grid.confidence["baseline"] < 0.3


# --- дополнительные тесты на утверждения, явно сформулированные текстом
# брифа (разведка, п.9-15), которых бриф не дал в виде готового кода ---

def test_no_guide_list_in_google_export_templates(profile_fixture):
    """p:guideLst отсутствует в обоих чистых Google-экспортах среди трёх
    учебных шаблонов (брифом, п.9; VK_WorkSpace — исключение, у него
    guideLst непустой, см. докстроку grid.py) — сетка обязана быть
    восстановлена кластеризацией, не направляющими."""
    for name in ("VK Tech шаблон.pptx", "Шаблон презентации VK Education.pptx"):
        assert profile_fixture(name).grid.native_guides_used is False


def test_vktech_column_verticals_are_found(profile_fixture):
    """VK Tech: карточные раскладки в 3-4 колонки дают вертикали на 63.61%
    и 81.56% (брифом, п.13)."""
    grid = profile_fixture("VK Tech шаблон.pptx").grid
    assert any(abs(axis - 0.6361) < 0.006 for axis in grid.columns)
    assert any(abs(axis - 0.8156) < 0.006 for axis in grid.columns)


def test_education_body_anchor_matches_measured_value(profile_fixture):
    """Верх основного текста у Education — 25.86% (брифом, п.15)."""
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert abs(grid.anchors["body_top"] - 0.2586) < 0.008


def _package_from_slide_xml(slide_xml: str) -> PptxPackage:
    """Синтетический пакет из одного slide1.xml — тот же приём, что и в
    tests/template/test_usage.py (_usage_from_slide_xml), для граничных
    случаев OOXML, которых нет ни в одном из трёх реальных шаблонов."""
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
    return PptxPackage(zipfile.ZipFile(buf, "r"))


_SLIDE_WITHOUT_XFRM = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="NoXfrm"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr/>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_skipped_shapes_without_box_are_counted():
    """Шейп без a:xfrm (box=None) для сетки не годится, но должен быть
    учтён, а не потеряться молча — на трёх реальных шаблонах такого шейпа
    нет вовсе (skipped_no_box==0 у всех), нужна синтетика с известным ответом."""
    pkg = _package_from_slide_xml(_SLIDE_WITHOUT_XFRM)
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    grid = build_grid(pkg, canvas)
    assert grid.skipped_no_box == 1


_GUIDE_WITH_MALFORMED_POS = """<?xml version="1.0" encoding="UTF-8"?>
<p:viewPr xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:slideViewPr><p:cSldViewPr><p:guideLst>
    <p:guide pos="not-a-number"/>
    <p:guide pos="3591"/>
  </p:guideLst></p:cSldViewPr></p:slideViewPr>
</p:viewPr>
"""


def test_malformed_guide_pos_is_skipped_not_fatal():
    """`p:guide/@pos` нечисловым не должен ронять разбор ВСЕЙ сетки —
    невалидная направляющая пропускается, остальные читаются как обычно
    (adversarial-reviewer, Task 4 повторное ревью)."""
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
        zf.writestr("ppt/viewProps.xml", _GUIDE_WITH_MALFORMED_POS)
    buf.seek(0)
    pkg = PptxPackage(zipfile.ZipFile(buf, "r"))
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    grid = build_grid(pkg, canvas)  # не должен бросить ValueError
    assert grid.native_guides_used is True
    assert len(grid.columns) == 1


def test_cluster_span_is_bounded_by_tolerance():
    """Регрессия на CRITICAL-находку adversarial-reviewer (Task 4 повторное
    ревью): раньше cluster() сравнивал каждую точку с ПОСЛЕДНЕЙ добавленной
    (классический single-linkage), из-за чего плотная цепочка точек
    (каждая ближе допуска только к соседней) могла "расползтись" на
    произвольно большой суммарный диапазон — на контрольном ЛЦТ2026 (не в
    тестах) это давало margin_left+margin_right>1, физически невозможную
    геометрию, с виду достоверную (confidence 0.4/0.9). Синтетика ниже не
    зависит от реального файла: 1000 точек с шагом чуть меньше допуска дают
    суммарный диапазон ~1.0 при допуске 0.01 — старая реализация схлопнула
    бы их в один кластер шириной ~1.0; новая обязана порезать на кластеры
    шириной не больше tolerance."""
    tol = 0.01
    step = tol * 0.9
    values = [i * step for i in range(1000)]
    clusters = cluster(values, tol)
    for c in clusters:
        assert max(c.members) - min(c.members) <= tol + 1e-9, c


def test_every_reported_number_carries_a_confidence_score(profile_fixture):
    """«Каждое число... должно нести меру уверенности» — margin_left/right/
    top/bottom, columns, gutter и baseline обязаны иметь запись в confidence."""
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    for key in ("margin_left", "margin_right", "margin_top", "margin_bottom",
                "columns", "gutter", "baseline"):
        assert key in grid.confidence
        assert 0.0 <= grid.confidence[key] <= 1.0
