"""Обход дерева шейпов слайда/лейаута/мастера — walk_shapes/shape_box.

Тесты на реальных шаблонах из dataset/templates/ (первые три; ЛЦТ2026 —
контрольный шаблон для защиты, в тестах не участвует, см. tests/ooxml/
test_package.py, где та же фильтрация).
"""
from __future__ import annotations
from pathlib import Path

import pytest
from lxml import etree

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes

TEMPLATES = sorted(
    p for p in Path("dataset/templates").glob("*.pptx")
    if not p.stem.startswith("ЛЦТ2026")
)

VK_TECH = Path("dataset/templates/VK Tech шаблон.pptx")
VK_WORKSPACE = Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")


def _canvas(pkg: PptxPackage) -> Canvas:
    root = pkg.xml("ppt/presentation.xml")
    sz = root.find(qn("p:sldSz"))
    return Canvas(width_emu=int(sz.get("cx")), height_emu=int(sz.get("cy")))


def _slide_names(pkg: PptxPackage) -> list[str]:
    return sorted(
        n for n in pkg.names()
        if n.startswith("ppt/slides/slide") and n.endswith(".xml")
    )


def _own_id(element) -> str | None:
    """Id из cNvPr собственного nv*Pr-обёртки элемента (не из вложенных шейпов)."""
    for child in element:
        if etree.QName(child).localname.startswith("nv"):
            cnvpr = child.find(qn("p:cNvPr"))
            return cnvpr.get("id") if cnvpr is not None else None
    return None


# 1. Обход слайда с группами отдаёт больше шейпов, чем len(spTree) верхнего уровня.
def test_descending_into_groups_yields_more_shapes_than_top_level():
    with PptxPackage.open(VK_TECH) as pkg:
        canvas = _canvas(pkg)
        root = pkg.xml("ppt/slides/slide21.xml")  # 4 группы верхнего уровня
        sp_tree = root.find(f"{qn('p:cSld')}/{qn('p:spTree')}")
        top_level = [
            el for el in sp_tree
            if etree.QName(el).localname in ("sp", "pic", "graphicFrame", "cxnSp", "grpSp")
        ]
        leaves = list(walk_shapes(root, canvas))
        assert len(leaves) > len(top_level)


# 2. Все box в разумных пределах на всех слайдах всех трёх шаблонов — тест-страж
#    на аффинное преобразование групп (до него встречались 244% и −0%).
#
#    Нижняя граница по width/height — >= 0, не строго > 0: у p:cxnSp
#    (соединительная линия) один из размеров легитимно равен нулю —
#    строго горизонтальная или строго вертикальная линия. Разведкой
#    подтверждено на реальных данных: VK Education — 12 нулевых width,
#    24 нулевых height; WorkSpace — 4 нулевых height. Это не брак
#    геометрии, а обычная прямая линия; строгое "> 0" из буквального текста
#    брифа не пережило бы прогон по реальным данным.
#
#    Верхняя граница по left/top — 2.0, не 1.5 из буквального текста
#    брифа: в VK Education есть два шейпа верхнего уровня (slide18/449,
#    slide28/795 — одна и та же запаркованная вне холста декоративная
#    фигура, left≈1.53, right≈1.89) без единой группы над ними — это не
#    цепочка аффинных преобразований, просто дизайнерский элемент, вынесенный
#    за пределы слайда. 2.0 всё ещё ловит исторический баг из брифа (244%,
#    т.е. 2.44) с запасом, но не роняет тест на реальном дизайнерском мусоре.
def test_all_boxes_are_within_sane_bounds_across_all_templates():
    checked = 0
    for path in TEMPLATES:
        with PptxPackage.open(path) as pkg:
            canvas = _canvas(pkg)
            for name in _slide_names(pkg):
                root = pkg.xml(name)
                for ref in walk_shapes(root, canvas, include_groups=True):
                    if ref.box is None:
                        continue
                    checked += 1
                    box = ref.box
                    assert -0.5 <= box.left <= 2.0, (path.name, name, ref.shape_id, box)
                    assert 0 <= box.width <= 2.0, (path.name, name, ref.shape_id, box)
                    assert -0.5 <= box.top <= 2.0, (path.name, name, ref.shape_id, box)
                    assert 0 <= box.height <= 2.0, (path.name, name, ref.shape_id, box)
    assert checked > 0


# 3. Ширина ребёнка группы масштабируется — конкретная группа id=419 (слайд3,
#    VK Tech), ребёнок id=420: ext.cx=189326 при group.ext.cx=1195387,
#    group.chExt.cx=2283170. Ожидаемая ширина посчитана вручную по формуле
#    child.ext.cx * (group.ext.cx / group.chExt.cx) / canvas.width_emu.
def test_group_child_width_scales_by_group_to_child_extent_ratio():
    with PptxPackage.open(VK_TECH) as pkg:
        canvas = _canvas(pkg)
        root = pkg.xml("ppt/slides/slide3.xml")
        refs = {ref.shape_id: ref for ref in walk_shapes(root, canvas)}
        ref = refs["420"]
        assert ref.box is not None
        expected_width = 189326 * (1195387 / 2283170) / canvas.width_emu
        # resolve_point округляет каждую угловую точку до целого EMU (см.
        # geometry.py); ширина здесь — разность двух независимо округлённых
        # точек, поэтому расходится с чистой алгебраической формулой на
        # долю EMU (тут 189326*1195387/2283170=99124.39, обе точки round()
        # дают ровно 99124 — расхождение 0.39 EMU, что на холсте 9144000 EMU
        # даёт ~4e-8 по доле). abs=1e-6 — на порядок больше этого шума.
        assert ref.box.width == pytest.approx(expected_width, abs=1e-6)


# 4. include_groups=True даёт строго больше элементов и группы приходят
#    раньше своего содержимого (предзаказный обход).
def test_include_groups_yields_groups_before_their_content():
    with PptxPackage.open(VK_TECH) as pkg:
        canvas = _canvas(pkg)
        root = pkg.xml("ppt/slides/slide21.xml")
        without = list(walk_shapes(root, canvas, include_groups=False))
        with_groups = list(walk_shapes(root, canvas, include_groups=True))
        assert len(with_groups) > len(without)

        ids = [ref.shape_id for ref in with_groups]
        kinds = {ref.shape_id: ref.kind for ref in with_groups}
        # группа id=907 — первая верхнеуровневая группа слайда21, её первый
        # ребёнок — p:pic id=908 (см. разведку сырого XML).
        assert kinds["907"] == "group"
        assert ids.index("907") < ids.index("908")


# 5. Шейп без a:xfrm приходит с box=None и не роняет обход. Ни в одном из трёх
#    шаблонов такого не нашлось (проверено разведкой: 0 шейпов без xfrm на
#    slides/layouts/masters всех трёх) — синтетический XML.
_SYNTH_NO_XFRM = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="2" name="WithXfrm"/><p:cNvSpPr/><p:nvPr/>
        </p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="100" y="200"/><a:ext cx="300" cy="400"/></a:xfrm>
        </p:spPr>
      </p:sp>
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="3" name="InheritedFromLayout"/><p:cNvSpPr/>
          <p:nvPr><p:ph idx="1"/></p:nvPr>
        </p:nvSpPr>
        <p:spPr/>
      </p:sp>
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="4" name="AfterTheGap"/><p:cNvSpPr/><p:nvPr/>
        </p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="500" y="600"/><a:ext cx="700" cy="800"/></a:xfrm>
        </p:spPr>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_shape_without_xfrm_yields_none_box_and_does_not_crash():
    root = etree.fromstring(_SYNTH_NO_XFRM.encode())
    canvas = Canvas(width_emu=9144000, height_emu=5143500)
    refs = {ref.shape_id: ref for ref in walk_shapes(root, canvas)}
    assert refs["2"].box is not None
    assert refs["3"].box is None
    # обход не остановился на шейпе без xfrm — следующий шейп дошёл и посчитан
    assert refs["4"].box is not None


# 6. ph_type для плейсхолдера без атрибута @type равен "body", а не None —
#    в трёх шаблонах такой плейсхолдер не встретился (все Google-экспорты
#    проставляют @type явно), реальная ловушка OOXML нужна синтетически.
def test_placeholder_without_type_attribute_defaults_to_body():
    root = etree.fromstring(_SYNTH_NO_XFRM.encode())
    canvas = Canvas(width_emu=9144000, height_emu=5143500)
    refs = {ref.shape_id: ref for ref in walk_shapes(root, canvas)}
    ph = refs["3"]
    assert ph.is_placeholder is True
    assert ph.ph_type == "body"
    assert ph.ph_idx == 1


# 7. Число TITLE-плейсхолдеров по всем 15 лейаутам VK WorkSpace — 15, по
#    одному на лейаут (число из разведки, см. бриф).
def test_layout_walk_finds_one_title_placeholder_per_layout():
    with PptxPackage.open(VK_WORKSPACE) as pkg:
        canvas = _canvas(pkg)
        layout_names = sorted(
            n for n in pkg.names()
            if n.startswith("ppt/slideLayouts/slideLayout") and n.endswith(".xml")
        )
        assert len(layout_names) == 15

        total_titles = 0
        for name in layout_names:
            root = pkg.xml(name)
            titles = [
                ref for ref in walk_shapes(root, canvas)
                if ref.is_placeholder and ref.ph_type == "title"
            ]
            assert len(titles) == 1, name
            total_titles += len(titles)
        assert total_titles == 15


# 8. Порядок обхода — документный: shape_id по слайду совпадает с порядком
#    в сыром XML (document-order DFS через lxml .iter()).
def test_walk_order_matches_raw_xml_document_order():
    with PptxPackage.open(VK_TECH) as pkg:
        canvas = _canvas(pkg)
        root = pkg.xml("ppt/slides/slide21.xml")
        sp_tree = root.find(f"{qn('p:cSld')}/{qn('p:spTree')}")

        raw_ids = [
            _own_id(el) for el in sp_tree.iter()
            if etree.QName(el).localname in ("sp", "pic", "cxnSp", "graphicFrame", "grpSp")
        ]
        walked_ids = [ref.shape_id for ref in walk_shapes(root, canvas, include_groups=True)]
        assert walked_ids == raw_ids
        assert len(raw_ids) > 0
