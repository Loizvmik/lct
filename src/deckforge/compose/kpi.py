"""Показатели с нуля: ряд крупных чисел с подписями для шаблона, у которого
нет раскладки показателей (VK Education и VK Tech по разбору с моделью).

Раньше планировщик ослаблял форму до карточек, и «31,5 ч → 6,2 ч» ложилось
строкой текста в карточку. Здесь число набирается кеглем display или h1
шаблона (самым крупным, при котором каждое значение встаёт в одну строку
своей ячейки), цветом бренда, подпись под ним кеглем body, ячейки ровным
рядом по сетке, и под каждой плашка карточки шаблона, если она у шаблона
есть (`builder.TemplateLook.card_plaque`).

Модуль только раскладывает: отдаёт места (`SlotContent`) и плашки
(`DecorShape`). Рисует, ужимает и красит их `builder.place_slide` тем же
путём, что и слоты раскладки, поэтому аудит видит обычные текстовые рамки."""
from __future__ import annotations
import math
from dataclasses import replace

from deckforge.compose.blocks import Paragraph, SlotContent
from deckforge.compose.textfit import measure
from deckforge.ooxml.geometry import Box
from deckforge.plan.spec import KpiBlock
from deckforge.template.patterns import DecorShape, PatternSlot

EMU_PER_INCH = 914400
# Показателей в ряду: пять и больше крупным кеглем в строку не встают,
# они идут вторым рядом.
MAX_PER_ROW = 4
MAX_ITEMS = 8
# Ступени шкалы шаблона под значение, от крупной к мелкой.
VALUE_STEPS = ("display", "h1", "h2")
# Значение не мельче стольких кеглей body: мельче это уже не показатель.
VALUE_MIN_BODY = 1.5
LABEL_LINES = 3
LINE_SPACING = 1.2
_SHRINK = 0.9
# Зазор между значением и подписью, доля высоты холста.
_VALUE_GAP = 0.01
# Поле текста внутри плашки не уже этой доли холста: у ЛЦТ2026 шаг сетки
# 0,6% ширины, и текст прилипал к краю плашки.
PLAQUE_PAD = 0.015
# Плашка ряда не ниже этой доли места: низкая полоса под крупными
# числами на пустом слайде читалась недозаполненной (D05).
PLAQUE_MIN_SHARE = 0.5


def line_height(profile, size_pt: float, lines: int = 1) -> float:
    """Высота `lines` строк кегля `size_pt` (реальные пункты) в долях
    высоты холста."""
    height_in = profile.canvas_height_emu / EMU_PER_INCH
    return lines * size_pt * LINE_SPACING / 72.0 / height_in


def width_in(profile, share: float) -> float:
    return share * profile.canvas_width_emu / EMU_PER_INCH


# Доля ширины рамки, по которой меряются строки: LibreOffice и PowerPoint
# переносят чуть раньше замера, и роль участника в три строки вылезала
# из плашки четвёртой.
_MEASURE_WIDTH_SHARE = 0.92


def lines_needed(texts, family: str, size_pt: float, width_inches: float) -> int:
    """Строк самого длинного из `texts` кеглем `size_pt` в ширину."""
    width = width_inches * _MEASURE_WIDTH_SHARE
    return max(
        (measure(t, family, size_pt, width, line_spacing=LINE_SPACING).lines for t in texts if t.strip()),
        default=1,
    )


def text_slot(role: str, box: Box, size_norm: float, *, align: str = "l", anchor: str = "t") -> PatternSlot:
    return PatternSlot(
        role=role, box=box, size_pt=size_norm, color_hex=None, align=align, max_chars=0, wraps=True,
        anchor=anchor,
    )


def plaque_at(plaque: DecorShape | None, box: Box) -> DecorShape | None:
    """Копия плашки карточки шаблона на месте `box`, вне группы повтора."""
    if plaque is None:
        return None
    return replace(plaque, box=box, repeat_group=False, repeat_index=0, badge_text=None, source_shape_id=None)


def plaque_pad(grid, plaque: DecorShape | None) -> float:
    return max(grid.gutter / 2, PLAQUE_PAD) if plaque is not None else 0.0


def grid_shape(n: int, per_row: int) -> tuple[int, int]:
    """(рядов, колонок) для `n` единиц не больше `per_row` в ряду: ряды
    поровну, чтобы шесть не легли четыре плюс два."""
    rows = max(1, math.ceil(n / per_row))
    return rows, math.ceil(n / rows)


def value_size(profile, family: str, values: list[str], cell_width_in: float) -> float:
    """Самый крупный кегль шкалы (display, h1, h2, дальше ужатый h2), при
    котором каждое значение встаёт в одну строку ячейки."""
    body = profile.type_scale_pt("body", 0.0) or 14.0
    floor = body * VALUE_MIN_BODY
    sizes = [s for s in (profile.type_scale_pt(step, 0.0) or 0.0 for step in VALUE_STEPS) if s > 0]
    size = min(sizes) if sizes else floor * 2
    while size * _SHRINK >= floor:
        size *= _SHRINK
        sizes.append(size)
    for size in sorted(set(sizes), reverse=True):
        if lines_needed(values, family, size, cell_width_in) <= 1:
            return size
    return floor


def kpi_layout(
    block: KpiBlock, region: Box, profile, grid, family: str, plaque: DecorShape | None = None,
) -> tuple[list[SlotContent], list[DecorShape]]:
    """Места и плашки ряда показателей в `region` (доли холста)."""
    items = [k for k in block.items if k.value.strip()][:MAX_ITEMS]
    if not items:
        return [], []
    rows, cols = grid_shape(len(items), MAX_PER_ROW)
    gutter = grid.gutter
    pad = plaque_pad(grid, plaque)
    cell_w = (region.width - gutter * (cols - 1)) / cols
    inner_w = max(cell_w - 2 * pad, 0.01)
    body_pt = profile.type_scale_pt("body", 0.0) or 14.0
    norm = profile.canvas_norm
    value_pt = value_size(profile, family, [k.value for k in items], width_in(profile, inner_w))
    label_lines = min(LABEL_LINES, lines_needed([k.label for k in items], family, body_pt, width_in(profile, inner_w)))
    row_budget = (region.height - (rows - 1) * gutter) / rows
    while True:
        value_h = line_height(profile, value_pt)
        label_h = line_height(profile, body_pt, label_lines)
        content_h = value_h + _VALUE_GAP + label_h
        if 2 * pad + content_h <= row_budget or (value_pt <= body_pt and label_lines <= 1):
            break
        if label_lines > 1:
            label_lines -= 1
        else:
            value_pt = max(body_pt, value_pt * _SHRINK)
    cell_h = 2 * pad + content_h
    if plaque is not None:
        cell_h = min(row_budget, max(cell_h, region.height * PLAQUE_MIN_SHARE / rows))
    total_h = rows * cell_h + (rows - 1) * gutter
    top0 = region.top + max(0.0, (region.height - total_h) / 2)
    contents: list[SlotContent] = []
    plaques: list[DecorShape] = []
    for i, kpi in enumerate(items):
        r, c = divmod(i, cols)
        left = region.left + c * (cell_w + gutter)
        top = top0 + r * (cell_h + gutter)
        shape = plaque_at(plaque, Box(left=left, top=top, width=cell_w, height=cell_h))
        if shape is not None:
            plaques.append(shape)
        # Число и подпись посередине ячейки по высоте: плашка выше их.
        inner_top = top + (cell_h - content_h) / 2
        value_box = Box(left=left + pad, top=inner_top, width=inner_w, height=value_h)
        label_box = Box(left=left + pad, top=value_box.bottom + _VALUE_GAP, width=inner_w, height=label_h)
        contents.append(SlotContent(text_slot("kpi_value", value_box, value_pt * norm), "kpi_value",
                                    [Paragraph(kpi.value.strip())]))
        if kpi.label.strip():
            contents.append(SlotContent(text_slot("kpi_label", label_box, body_pt * norm), "kpi_label",
                                        [Paragraph(kpi.label.strip())]))
    return contents, plaques
