"""Чернила слайда: какая доля холста реально занята видимым содержанием.

Одна мера на двух потребителей. Аудит (`audit.deterministic`, D05 и L02)
судит по ней готовый слайд, разбор шаблона (`template.patterns`) снимает
ту же долю со слайда-примера (`Pattern.source_density`). Сравнивать
заполненность собранного слайда с его примером имеет смысл только когда
обе цифры получены одним и тем же счётом: разные счёты дали бы дельту
из-за метода, а не из-за содержания.

Модуль лежит отдельно от обоих пакетов намеренно: шаблон не должен
зависеть от аудита, а аудит уже зависит от шаблона. Сам он опирается
только на `ooxml` (разбор фигур) и `compose.textfit` (замер текста
шрифтом): замер текста это метрика шрифта, а не решение сборки."""
from __future__ import annotations
from typing import Iterable, Protocol

from deckforge.compose.textfit import measure
from deckforge.ooxml.color import Color, resolve_color
from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn

EMU_PER_INCH = 914400

# Тот же дефолт интерлиньяжа, что и `textfit._DEFAULT_LINE_SPACING`:
# абзац без явного `a:lnSpc` меряется им.
DEFAULT_LINE_SPACING = 1.2

# Дефолты ECMA-376 Part 1, §21.1.2.1.1 (CT_TextBodyProperties) для
# lIns/rIns/tIns/bIns, когда атрибут в разметке отсутствует: 0.1″/0.1″/
# 0.05″/0.05″ (в EMU). Наша сборка обнуляет их явно (`compose/builder.py::
# _draw_slot`), но аудит читает и чужие файлы, а слайд-пример шаблона
# почти всегда живёт на дефолте. Без вычитания полей замер видит больше
# места, чем реально доступно тексту.
_DEFAULT_LINS_EMU = 91440
_DEFAULT_TINS_EMU = 45720
_DEFAULT_RINS_EMU = 91440
_DEFAULT_BINS_EMU = 45720

_FILL_TAGS = ("a:noFill", "a:solidFill", "a:gradFill", "a:grpFill")


class InkShape(Protocol):
    """Всё, что мере нужно знать о фигуре. `audit.deterministic._Item`
    подходит как есть, разбор шаблона собирает `SimpleInk`."""

    kind: str  # "shape" | "picture" | "graphic_frame" | "connector"
    element: object
    box: Box
    text: str
    has_fill: bool


class SimpleInk:
    __slots__ = ("kind", "element", "box", "text", "has_fill")

    def __init__(self, kind: str, element, box: Box, text: str, has_fill: bool) -> None:
        self.kind, self.element, self.box, self.text, self.has_fill = kind, element, box, text, has_fill


def find_fill_node(container):
    for tag in _FILL_TAGS:
        el = container.find(qn(tag))
        if el is not None:
            return el
    return None


def shape_has_fill(element, scheme: dict, clr_map: dict) -> bool:
    """Своя непрозрачная сплошная заливка у автофигуры (`p:spPr`)."""
    sp_pr = element.find(qn("p:spPr"))
    fill_node = find_fill_node(sp_pr) if sp_pr is not None else None
    if fill_node is None:
        return False
    return isinstance(resolve_color(fill_node, scheme, clr_map), Color)


def text_frame_insets_in(sp_element) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) внутренних полей `a:bodyPr` в дюймах:
    явное значение атрибута, если задано, иначе дефолт спецификации.
    Отсутствие `p:txBody`/`a:bodyPr` даёт тот же дефолт: текст без явной
    рамки всё равно рисуется с полями."""
    tx_body = sp_element.find(qn("p:txBody"))
    body_pr = tx_body.find(qn("a:bodyPr")) if tx_body is not None else None

    def _inset(attr: str, default_emu: int) -> float:
        raw = body_pr.get(attr) if body_pr is not None else None
        if raw is None:
            return default_emu / EMU_PER_INCH
        try:
            return int(raw) / EMU_PER_INCH
        except ValueError:
            return default_emu / EMU_PER_INCH

    return (
        _inset("lIns", _DEFAULT_LINS_EMU), _inset("tIns", _DEFAULT_TINS_EMU),
        _inset("rIns", _DEFAULT_RINS_EMU), _inset("bIns", _DEFAULT_BINS_EMU),
    )


def dominant_run_style(sp_element) -> tuple[str, float, bool] | None:
    """(гарнитура, кегль pt, полужирность) самого "весомого" run'а фигуры:
    голосование по числу символов, тот же приём, что и `template/patterns.
    py::_shape_dominant_size`. `None`, если ни у одного run'а нет явных
    кегля и гарнитуры."""
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return None
    votes: dict[tuple[str, float, bool], int] = {}
    for r in tx_body.iter(qn("a:r")):
        r_pr = r.find(qn("a:rPr"))
        if r_pr is None:
            continue
        sz_raw = r_pr.get("sz")
        latin = r_pr.find(qn("a:latin"))
        family = latin.get("typeface") if latin is not None else None
        if sz_raw is None or not family:
            continue
        size_pt = int(sz_raw) / 100
        bold = r_pr.get("b") == "1"
        t_el = r.find(qn("a:t"))
        chars = len(t_el.text or "") if t_el is not None else 0
        key = (family, size_pt, bold)
        votes[key] = votes.get(key, 0) + max(chars, 1)
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def first_paragraph_line_spacing(sp_element) -> float:
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return DEFAULT_LINE_SPACING
    p_el = tx_body.find(qn("a:p"))
    if p_el is None:
        return DEFAULT_LINE_SPACING
    p_pr = p_el.find(qn("a:pPr"))
    if p_pr is None:
        return DEFAULT_LINE_SPACING
    ln_spc = p_pr.find(qn("a:lnSpc"))
    if ln_spc is None:
        return DEFAULT_LINE_SPACING
    pct = ln_spc.find(qn("a:spcPct"))
    if pct is None or pct.get("val") is None:
        return DEFAULT_LINE_SPACING
    return int(pct.get("val")) / 100000.0


def ink_box(item: InkShape, canvas: Canvas) -> Box:
    """Площадь текстового блока ИЗМЕРЕННАЯ (`textfit.measure`), а не
    объявленная: рамки намеренно держат запас по высоте, и объявленная
    рамка дала бы ложные наложения и завышенную заполненность. Высота
    ограничена сверху объявленной (переполнение ловят L03/L04).

    Усадка не применяется к фигуре со своей заливкой: плашка даёт видимые
    чернила на всю рамку, сколько бы места ни занимал текст внутри. Ширина
    и высота замера берутся за вычетом внутренних полей рамки, итоговая
    высота добавляет поля обратно."""
    if item.kind != "shape" or not item.text.strip() or item.has_fill:
        return item.box
    style = dominant_run_style(item.element)
    if style is None:
        return item.box
    family, size_pt, _bold = style
    l_in, t_in, r_in, b_in = text_frame_insets_in(item.element)
    box_width_in = max(0.0, item.box.width * canvas.width_in - l_in - r_in)
    line_spacing = first_paragraph_line_spacing(item.element)
    metrics = measure(item.text, family, size_pt, box_width_in, line_spacing=line_spacing)
    declared_full_height_in = item.box.height * canvas.height_in
    available_height_in = max(0.0, declared_full_height_in - t_in - b_in)
    effective_content_height_in = min(metrics.height_in, available_height_in)
    effective_height_in = min(effective_content_height_in + t_in + b_in, declared_full_height_in)
    effective_height = effective_height_in / canvas.height_in if canvas.height_in else item.box.height
    return Box(left=item.box.left, top=item.box.top, width=item.box.width, height=effective_height)


def ink_ratio(items: Iterable[InkShape], canvas: Canvas) -> float:
    """Доля холста под чернилами, 0..1: текст по измеренной высоте, плашки
    со своей заливкой, картинки, таблицы и графики целиком.

    Площадь считается объединением коробок, обрезанных холстом. До задачи R
    D05 складывал площади, и текст на своей плашке шёл дважды: слайд-пример
    ЛЦТ2026 с двумя карточками на подложках давал 125% холста. Сравнивать
    клон с примером по такой мере значит сравнивать число наложений, а не
    заполненность."""
    if canvas.width_in * canvas.height_in <= 0:
        return 0.0
    boxes = []
    for item in items:
        if item.kind == "connector":
            continue
        if item.kind == "shape" and (item.text.strip() or item.has_fill):
            boxes.append(ink_box(item, canvas))
        elif item.kind in ("picture", "graphic_frame"):
            boxes.append(item.box)
    return _union_area(boxes)


def _union_area(boxes: list[Box]) -> float:
    """Площадь объединения прямоугольников в долях холста, обрезанных по
    [0, 1]: сжатие координат по X, в каждой полосе слияние отрезков по Y.
    Фигур на слайде десятки, квадратичный счёт тут дешевле любой структуры."""
    rects = []
    for b in boxes:
        left, right = max(0.0, b.left), min(1.0, b.left + b.width)
        top, bottom = max(0.0, b.top), min(1.0, b.top + b.height)
        if right > left and bottom > top:
            rects.append((left, right, top, bottom))
    if not rects:
        return 0.0
    xs = sorted({x for r in rects for x in (r[0], r[1])})
    area = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        spans = sorted((r[2], r[3]) for r in rects if r[0] <= x0 and r[1] >= x1)
        covered, cur_top, cur_bottom = 0.0, None, None
        for top, bottom in spans:
            if cur_bottom is None or top > cur_bottom:
                if cur_bottom is not None:
                    covered += cur_bottom - cur_top
                cur_top, cur_bottom = top, bottom
            else:
                cur_bottom = max(cur_bottom, bottom)
        if cur_bottom is not None:
            covered += cur_bottom - cur_top
        area += covered * (x1 - x0)
    return area
