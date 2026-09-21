"""Обход дерева шейпов слайда/лейаута/мастера.

Отдельный модуль, а не часть geometry.py: geometry.py — чистая аффинная
математика (Canvas/GroupFrame/resolve_point/Box), без знания об устройстве
дерева OOXML-шейпов (nv*Pr-обёртки, p:ph, разница p:xfrm у graphicFrame и
a:xfrm у остальных). walk.py — про форму дерева и извлечение метаданных
шейпа, строится поверх geometry.py как потребитель её примитивов. Это
удерживает geometry.py маленьким и не смешивает два разных типа знания в
одном файле.

python-pptx не даёт единообразного способа обойти p:sp/p:pic/p:graphicFrame/
p:cxnSp/p:grpSp с сохранением документного порядка и точным доступом к сырым
атрибутам xfrm/ph — поэтому обход написан прямо по lxml-дереву.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from lxml import etree

from deckforge.ooxml.geometry import Box, Canvas, GroupFrame, box_from_emu, resolve_point
from deckforge.ooxml.ns import qn

# Локальные имена элементов-шейпов OOXML → наш kind. p:grpSp — тоже шейп
# (может быть плейсхолдером-группой, хотя на практике не встречается), но
# отдаётся только при include_groups=True.
_KIND_BY_TAG = {
    "sp": "shape",
    "pic": "picture",
    "graphicFrame": "graphic_frame",
    "cxnSp": "connector",
    "grpSp": "group",
}

# Корневые элементы, которые walk_shapes принимает напрямую помимо p:spTree.
_ROOTS_WITH_CSLD = {"sld", "sldLayout", "sldMaster"}

# Группа без валидного xfrm (в трёх учебных шаблонах не встретилась ни разу —
# разведкой проверено, что у каждого grpSp есть полный off/ext/chOff/chExt) —
# тождественное преобразование, чтобы обход не падал и не выдумывал сдвиг.
_IDENTITY_GROUP_FRAME = GroupFrame(off=(0, 0), ext=(1, 1), ch_off=(0, 0), ch_ext=(1, 1))


@dataclass(frozen=True)
class ShapeRef:
    element: Any          # lxml-элемент шейпа: p:sp, p:pic, p:graphicFrame, p:cxnSp, p:grpSp
    kind: str              # "shape" | "picture" | "graphic_frame" | "connector" | "group"
    box: Box | None        # доли холста, аффинное преобразование групп уже применено;
                            # None, если у шейпа нет a:xfrm (координаты наследуются от
                            # плейсхолдера лейаута — резолв наследования вне этого слоя)
    name: str               # p:cNvPr/@name, пустая строка если нет
    shape_id: str           # p:cNvPr/@id
    rotation: float          # градусы (a:xfrm/@rot — в OOXML 1/60000 градуса)
    flip_h: bool
    flip_v: bool
    group_depth: int         # 0 для шейпа верхнего уровня
    group_chain: tuple[GroupFrame, ...]
    is_placeholder: bool      # есть ли p:nvSpPr/p:nvPr/p:ph (или его аналог для pic/graphicFrame/...)
    ph_type: str | None       # p:ph/@type; None если не плейсхолдер. Отсутствие @type у
                                # присутствующего p:ph по OOXML означает тип "body", не «нет типа»
    ph_idx: int | None        # p:ph/@idx


def _sp_tree_of(tree_root: etree._Element) -> etree._Element:
    """p:sld / p:sldLayout / p:sldMaster / p:spTree → p:spTree.

    walk_shapes принимает любой из четырёх — определяется по локальному имени
    корневого тега.
    """
    local = etree.QName(tree_root).localname
    if local == "spTree":
        return tree_root
    if local in _ROOTS_WITH_CSLD:
        c_sld = tree_root.find(qn("p:cSld"))
        sp_tree = c_sld.find(qn("p:spTree")) if c_sld is not None else None
        if sp_tree is None:
            raise ValueError(f"{local}: не найден p:cSld/p:spTree")
        return sp_tree
    raise ValueError(
        f"walk_shapes: неожиданный корневой элемент {local!r}, "
        "ожидался p:sld/p:sldLayout/p:sldMaster/p:spTree"
    )


def _nv_wrapper(element: etree._Element) -> etree._Element | None:
    """Первый ребёнок с именем nv*Pr (nvSpPr/nvPicPr/nvGraphicFramePr/nvCxnSpPr/nvGrpSpPr).

    По схеме OOXML это всегда первый дочерний элемент шейпа.
    """
    for child in element:
        if etree.QName(child).localname.startswith("nv"):
            return child
    return None


def _xfrm_element(element: etree._Element, kind: str) -> etree._Element | None:
    """Находит узел трансформации: p:xfrm у graphicFrame (напрямую), a:xfrm
    внутри p:spPr у shape/picture/connector, a:xfrm внутри p:grpSpPr у group."""
    if kind == "graphic_frame":
        return element.find(qn("p:xfrm"))
    container_tag = "p:grpSpPr" if kind == "group" else "p:spPr"
    container = element.find(qn(container_tag))
    if container is None:
        return None
    return container.find(qn("a:xfrm"))


def shape_box(element: etree._Element, canvas: Canvas, chain: Sequence[GroupFrame]) -> Box | None:
    """Box шейпа в долях холста с уже применённой цепочкой групп.

    Читает a:off/a:ext из xfrm шейпа, прогоняет обе угловые точки (левый
    верхний и правый нижний углы) через resolve_point с той же цепочкой —
    так же масштабируется и размер, а не только положение: у группы ext и
    chExt различаются, и ширина ребёнка обязана сжаться в той же пропорции,
    что и его положение. Нет a:xfrm — None.
    """
    kind = _KIND_BY_TAG.get(etree.QName(element).localname)
    if kind is None:
        return None
    xfrm = _xfrm_element(element, kind)
    if xfrm is None:
        return None
    off = xfrm.find(qn("a:off"))
    ext = xfrm.find(qn("a:ext"))
    if off is None or ext is None:
        return None

    left, top = int(off.get("x")), int(off.get("y"))
    width, height = int(ext.get("cx")), int(ext.get("cy"))

    chain_list = list(chain)
    x1, y1 = resolve_point(left, top, chain_list)
    x2, y2 = resolve_point(left + width, top + height, chain_list)
    return box_from_emu(x1, y1, x2 - x1, y2 - y1, canvas)


def _read_group_frame(element: etree._Element) -> GroupFrame:
    container = element.find(qn("p:grpSpPr"))
    xfrm = container.find(qn("a:xfrm")) if container is not None else None
    if xfrm is None:
        return _IDENTITY_GROUP_FRAME

    off = xfrm.find(qn("a:off"))
    ext = xfrm.find(qn("a:ext"))
    ch_off = xfrm.find(qn("a:chOff"))
    ch_ext = xfrm.find(qn("a:chExt"))
    if off is None or ext is None or ch_off is None or ch_ext is None:
        return _IDENTITY_GROUP_FRAME

    return GroupFrame(
        off=(int(off.get("x")), int(off.get("y"))),
        ext=(int(ext.get("cx")), int(ext.get("cy"))),
        ch_off=(int(ch_off.get("x")), int(ch_off.get("y"))),
        ch_ext=(int(ch_ext.get("cx")), int(ch_ext.get("cy"))),
    )


def _shape_ref(
    element: etree._Element, kind: str, canvas: Canvas,
    chain: tuple[GroupFrame, ...], depth: int,
) -> ShapeRef:
    nv = _nv_wrapper(element)
    cnvpr = nv.find(qn("p:cNvPr")) if nv is not None else None
    name = cnvpr.get("name", "") if cnvpr is not None else ""
    shape_id = cnvpr.get("id", "") if cnvpr is not None else ""

    nv_pr = nv.find(qn("p:nvPr")) if nv is not None else None
    ph = nv_pr.find(qn("p:ph")) if nv_pr is not None else None
    is_placeholder = ph is not None
    ph_type = ph.get("type", "body") if ph is not None else None
    ph_idx_raw = ph.get("idx") if ph is not None else None
    ph_idx = int(ph_idx_raw) if ph_idx_raw is not None else None

    xfrm = _xfrm_element(element, kind)
    rotation = 0.0
    flip_h = False
    flip_v = False
    if xfrm is not None:
        rot_raw = xfrm.get("rot")
        rotation = int(rot_raw) / 60000 if rot_raw is not None else 0.0
        flip_h = xfrm.get("flipH") == "1"
        flip_v = xfrm.get("flipV") == "1"

    return ShapeRef(
        element=element,
        kind=kind,
        box=shape_box(element, canvas, chain),
        name=name,
        shape_id=shape_id,
        rotation=rotation,
        flip_h=flip_h,
        flip_v=flip_v,
        group_depth=depth,
        group_chain=chain,
        is_placeholder=is_placeholder,
        ph_type=ph_type,
        ph_idx=ph_idx,
    )


def _walk_container(
    container: etree._Element, canvas: Canvas,
    chain: tuple[GroupFrame, ...], depth: int, include_groups: bool,
) -> Iterator[ShapeRef]:
    for element in container:
        kind = _KIND_BY_TAG.get(etree.QName(element).localname)
        if kind is None:
            continue  # p:nvGrpSpPr/p:grpSpPr и прочее не по кейсу kind-таблицы

        ref = _shape_ref(element, kind, canvas, chain, depth)
        if kind == "group":
            if include_groups:
                yield ref
            child_chain = chain + (_read_group_frame(element),)
            yield from _walk_container(element, canvas, child_chain, depth + 1, include_groups)
        else:
            yield ref


def walk_shapes(
    tree_root: etree._Element, canvas: Canvas, *, include_groups: bool = False,
) -> Iterator[ShapeRef]:
    """Обходит дерево шейпов слайда/лейаута/мастера в документном порядке.

    `tree_root` — p:sld, p:sldLayout, p:sldMaster (спускается в p:cSld/p:spTree
    автоматически) либо сам p:spTree.

    Рекурсивно спускается в p:grpSp, накапливая цепочку GroupFrame и применяя
    её к box каждого потомка через shape_box. Порядок — документный (z-order
    снизу вверх), предзаказный: группа отдаётся раньше своего содержимого.

    По умолчанию отдаёт только листья (include_groups=False). При
    include_groups=True отдаёт и сами группы (kind="group") до их содержимого,
    чтобы вызывающий мог пропустить поддерево.
    """
    sp_tree = _sp_tree_of(tree_root)
    yield from _walk_container(sp_tree, canvas, (), 0, include_groups)
