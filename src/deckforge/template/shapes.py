"""Словарь автофигур шаблона — гистограмма `a:prstGeom` по всем шейпам пакета.

Нужен `compose/diagrams.py` (Task 10): схемы (process/cycle/hierarchy/...)
обязаны рисовать карточки соединений формами, которые реально есть в
шаблоне, а не скруглёнными карточками по вкусу генератора — брифом задачи
дословно: «Словарь форм у шаблонов разный. VK Tech: прямоугольник 1359,
скруглённый 176, овал 18. WorkSpace: скруглённый 127, прямоугольник 38,
овал 8. Education: овал 100, скруглённый 27, прямоугольник 9. Рисовать
скруглённые карточки там, где в шаблоне только прямые углы, — выход из
дизайн-системы».

Разведка (см. task-10-brief.md) нашла на всех четырёх шаблонах только три
преста автофигур в заметном количестве — `rect`/`roundRect`/`ellipse` (плюс
единичные декоративные формы, не годные для карточек диаграммы). Этот
модуль строит полную гистограмму (на будущее — вдруг найдётся четвёртый
преста на контрольном шаблоне), но `compose/diagrams.py` опирается только
на пересечение с `KNOWN_AUTOSHAPE_PRESETS`.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes

# Пресеты, которыми реально пользуется compose/diagrams.py — единственные
# три, что разведка нашла в заметном числе на всех четырёх шаблонах (см.
# докстроку модуля). Другие пресеты попадают в гистограмму, но диаграммы их
# не используют — не поддержаны разметкой скруглений/геометрии ниже.
KNOWN_AUTOSHAPE_PRESETS = ("rect", "roundRect", "ellipse")


@dataclass(frozen=True)
class ShapeVocabEntry:
    prst: str
    count: int
    # Степень скругления угла у roundRect — среднее значение параметра
    # скругления (`a:avLst/a:gd[@name="adj"]/@fmla`, доля 0..1 от меньшей
    # стороны фигуры согласно ECMA-376 §20.1.9.13 CT_PresetGeometry2D)
    # среди вхождений этого prst с непустым avLst. 0.0 у форм без параметра
    # скругления (rect, ellipse) или когда avLst не задан (шаблонное
    # значение самого prst).
    avg_adj: float = 0.0


def build_shape_vocabulary(pkg: PptxPackage, canvas: Canvas) -> list[ShapeVocabEntry]:
    """Гистограмма `prst` автофигур (`p:sp`, не коннекторы/картинки/
    таблицы) по всем слайдам/лейаутам/мастерам пакета, по убыванию частоты."""
    counts: Counter[str] = Counter()
    adj_sums: dict[str, list[float]] = {}
    for part_name in _shape_bearing_parts(pkg):
        root = pkg.xml(part_name)
        for shape_ref in walk_shapes(root, canvas, include_groups=False):
            if shape_ref.kind != "shape":
                continue
            prst, adj = _prst_geom(shape_ref.element)
            if prst is None:
                continue
            counts[prst] += 1
            if adj is not None:
                adj_sums.setdefault(prst, []).append(adj)

    entries = [
        ShapeVocabEntry(
            prst=prst, count=count,
            avg_adj=(sum(adj_sums[prst]) / len(adj_sums[prst])) if prst in adj_sums else 0.0,
        )
        for prst, count in counts.items()
    ]
    entries.sort(key=lambda e: -e.count)
    return entries


def _shape_bearing_parts(pkg: PptxPackage) -> list[str]:
    prefixes = ("ppt/slideLayouts/slideLayout", "ppt/slideMasters/slideMaster", "ppt/slides/slide")
    return sorted(
        name for name in pkg.names()
        if name.endswith(".xml") and any(name.startswith(p) for p in prefixes)
    )


def _prst_geom(element) -> tuple[str | None, float | None]:
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return None, None
    geom = sp_pr.find(qn("a:prstGeom"))
    if geom is None:
        return None, None
    prst = geom.get("prst")
    if not prst:
        return None, None

    adj: float | None = None
    av_lst = geom.find(qn("a:avLst"))
    if av_lst is not None:
        for gd in av_lst.findall(qn("a:gd")):
            if gd.get("name") != "adj":
                continue
            fmla = (gd.get("fmla") or "").split()
            if len(fmla) == 2 and fmla[0] == "val":
                try:
                    adj = int(fmla[1]) / 100000
                except ValueError:
                    adj = None
            break
    return prst, adj
