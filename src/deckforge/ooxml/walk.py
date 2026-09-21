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


@dataclass(frozen=True)
class ShapeRef:
    element: Any          # lxml-элемент шейпа: p:sp, p:pic, p:graphicFrame, p:cxnSp, p:grpSp
    kind: str              # "shape" | "picture" | "graphic_frame" | "connector" | "group"
    box: Box | None        # доли холста, аффинное преобразование групп уже применено;
                            # None по двум независимым причинам: (1) у шейпа нет собственного
                            # a:xfrm, и координаты должны наследоваться от плейсхолдера лейаута —
                            # резолв наследования вне этого слоя; (2) группа (или предок выше)
                            # сломана (нет валидного a:xfrm), и координаты шейпа разместить на
                            # слайде нельзя ни наследованием, ни восстановлением. Из ShapeRef
                            # невозможно отличить одну причину от другой — понадобится отдельное
                            # поле, если это различие критично для вызывающего
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


def _read_group_frame(element: etree._Element) -> GroupFrame | None:
    """GroupFrame группы, либо `None`, если у неё нет валидного `a:xfrm`.

    Невалиден: `a:xfrm` нет вовсе; нет хотя бы одного из `a:off`/`a:ext`/
    `a:chOff`/`a:chExt`; либо `chExt` равен нулю хотя бы по одной оси —
    не только по обеим сразу. `resolve_point` уже не делится на ноль в
    этом случае (там есть защита), но при `chExt`=0 по одной оси масштаб
    по ней становится 0.0, и все точки этой оси схлопываются в одну и ту
    же координату — точно такая же тихая порча координат, как и для
    полностью отсутствующего `xfrm`, просто по одной оси вместо обеих.
    Раньше эта функция отдавала тождественное преобразование как фолбэк —
    это молча протаскивало координаты детей в системе координат группы
    (ровно тот мусор, ради устранения которого сделана вся задача).
    Вызывающий код (`_walk_container`) на `None` отдаёт `box=None` и самой
    группе, и всему её поддереву, но продолжает обход.
    """
    container = element.find(qn("p:grpSpPr"))
    xfrm = container.find(qn("a:xfrm")) if container is not None else None
    if xfrm is None:
        return None

    off = xfrm.find(qn("a:off"))
    ext = xfrm.find(qn("a:ext"))
    ch_off = xfrm.find(qn("a:chOff"))
    ch_ext = xfrm.find(qn("a:chExt"))
    if off is None or ext is None or ch_off is None or ch_ext is None:
        return None

    ch_ext_x, ch_ext_y = int(ch_ext.get("cx")), int(ch_ext.get("cy"))
    if ch_ext_x == 0 or ch_ext_y == 0:
        return None

    return GroupFrame(
        off=(int(off.get("x")), int(off.get("y"))),
        ext=(int(ext.get("cx")), int(ext.get("cy"))),
        ch_off=(int(ch_off.get("x")), int(ch_off.get("y"))),
        ch_ext=(ch_ext_x, ch_ext_y),
    )


def _shape_ref(
    element: etree._Element, kind: str, canvas: Canvas,
    chain: tuple[GroupFrame, ...], depth: int, *, force_none_box: bool = False,
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
        box=None if force_none_box else shape_box(element, canvas, chain),
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
    chain: tuple[GroupFrame, ...], depth: int, include_groups: bool, *, broken: bool = False,
) -> Iterator[ShapeRef]:
    """`broken=True` — где-то выше по цепочке уже встретилась группа без
    валидного `a:xfrm`: всё поддерево получает `box=None`, но обход не
    останавливается — шейпы по-прежнему находятся и отдаются, просто без
    координат (см. `_read_group_frame`)."""
    for element in container:
        kind = _KIND_BY_TAG.get(etree.QName(element).localname)
        if kind is None:
            continue  # p:nvGrpSpPr/p:grpSpPr и прочее не по кейсу kind-таблицы

        if kind == "group":
            frame = _read_group_frame(element)
            group_broken = broken or frame is None
            ref = _shape_ref(element, kind, canvas, chain, depth, force_none_box=group_broken)
            if include_groups:
                yield ref
            child_chain = chain if frame is None else chain + (frame,)
            yield from _walk_container(
                element, canvas, child_chain, depth + 1, include_groups, broken=group_broken,
            )
        else:
            ref = _shape_ref(element, kind, canvas, chain, depth, force_none_box=broken)
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

    ShapeRef.box == None указывает, что координаты шейпа либо наследуются от
    плейсхолдера лейаута (у шейпа нет a:xfrm), либо не могут быть вычислены,
    так как группа-предок сломана (нет валидного a:xfrm) — см. описание
    ShapeRef.box для деталей. Из самого ShapeRef отличить оба случая нельзя.
    """
    sp_tree = _sp_tree_of(tree_root)
    yield from _walk_container(sp_tree, canvas, (), 0, include_groups)
