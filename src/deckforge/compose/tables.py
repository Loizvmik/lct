"""Нативные таблицы поверх `python-pptx` (`GraphicFrame` с `a:tbl`).

## Почему кегль подбирается циклом, а не фиксируется

Высота строки в `.pptx` (`a:tr/@h`) — МИНИМУМ, а не заданный размер: сам
формат обязывает клиента вырастить строку под содержимое, но никогда не
разрешает ужать её ниже заданного (ECMA-376 §21.1.3.14 CT_TableRow:
`h` — "the minimum height of the row"). Если не измерить высоту КАЖДОЙ
строки текущим кеглем ДО расстановки, а просто разделить высоту рамки
поровну, PowerPoint/LibreOffice молча раздуют строки с длинным текстом
сверх запланированного, и таблица уедет за нижний край слайда без единой
ошибки при сборке — находка, которую тесты не ловят (`frame.has_table`
остаётся `True`), а видно только глазами на рендере. Поэтому высота каждой
строки здесь — честный замер через `textfit.measure` (единственный расчёт
текста в проекте), не равное деление.

## Почему стиль таблицы не берётся из шаблона

У VK Tech `tableStyles.xml` отсутствует вовсе, у WorkSpace один стиль, у
Education два — вставить таблицу и положиться на готовый стиль шаблона не
получится ни на одном из трёх (либо стиля нет, либо непонятно, тот ли он
для этого контента). Поэтому `add_table` красит КАЖДУЮ ячейку явной
заливкой/цветом текста поверх дефолтного `tableStyleId` (сам он остаётся в
XML как обязательный по схеме атрибут, но прямое форматирование ячейки
перекрывает стиль визуально) — таблица опирается только на
`profile.palette_roles`, а не на то, что нашлось или не нашлось в
`tableStyles.xml` этого конкретного файла.
"""
from __future__ import annotations
from dataclasses import dataclass

from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from deckforge.compose.colorpick import (
    best_contrast_text_color, slide_background_luminance, surface_pair_for_luminance,
)
from deckforge.compose.textfit import measure
from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.template.profile import TemplateProfile

EMU_PER_INCH = 914400

_ALIGN_MAP = {"l": PP_ALIGN.LEFT, "ctr": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}

# Внутренние поля ячейки сверху/снизу — то же значение, что дефолт
# `python-pptx`/PowerPoint для `a:tcPr` (`marT`/`marB` = 45720 EMU = 0.05″),
# используется при замере высоты строки: без него посчитанная по тексту
# высота была бы теснее того, что реально покажет редактор, и строка всё
# равно подрастала бы неожиданно на живом файле.
_CELL_VPAD_IN = 0.05
# Боковые поля ячейки: дефолт `a:tcPr` (`marL`/`marR` = 91440 EMU = 0.1″).
_CELL_HPAD_IN = 0.1

# Нижний предел кегля таблицы при принудительном ужимании — доля от
# ступени "caption" типографической шкалы шаблона. caption — уже самый
# мелкий кегль дизайн-системы, для которого шаблон вообще что-то заявляет;
# уменьшать его ЕЩЁ на четверть — практический потолок разборчивости на
# проекции (типографское правило "не мельче 3/4 базового мелкого кегля" для
# вспомогательного текста). Ниже этого таблица должна была бы не влезать по
# СОДЕРЖАНИЮ (числу строк/длине текста), а не по кеглю — но функция обязана
# вернуть рабочую таблицу, а не бросить исключение, поэтому предел всё же
# есть, а не "ужимай, пока не влезет любой ценой".
FONT_FLOOR_RATIO = 0.75

# Множитель уменьшения кегля за один шаг цикла подбора — 10%, тот же
# порядок убывания, что `builder._SHRINK_STEPS` использует между соседними
# ступенями типографической шкалы (h1→h2→body→caption — падение на
# 15-30%), не откалиброван отдельно под таблицы.
_SHRINK_FACTOR = 0.9


@dataclass(frozen=True)
class TableSpec:
    header: list[str]
    rows: list[list[str]]
    # Выравнивание по столбцу: "l"/"ctr"/"r", по одному на каждый столбец
    # header. None — все столбцы по левому краю (типографская норма для
    # таблиц с текстом; числовые столбцы вызывающий код выравнивает по
    # правому краю явно).
    align: list[str] | None = None


def add_table(slide, box: Box, spec: TableSpec, profile: TemplateProfile, *, floor_pt: float | None = None):
    """Строит нативную таблицу в `box` (доли холста) на `slide`. Возвращает
    `GraphicFrame` (`.table`, `.has_table` — интерфейс `python-pptx`).
    `floor_pt`: нижний предел кегля вместо `FONT_FLOOR_RATIO * caption`
    (таблица-единственный блок слайда держит ступень caption)."""
    if not spec.header:
        raise ValueError("TableSpec.header пуст — таблице нечем озаглавить столбцы")
    n_cols = len(spec.header)
    all_rows: list[list[str]] = [list(spec.header)] + [list(r) for r in spec.rows]
    n_rows = len(all_rows)

    left, top, width, height = _emu_box(box, profile)
    frame = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
    table = frame.table
    _neutralise_style_banding(table)

    family = profile.type_scale.families[0] if profile.type_scale.families else "Arial"
    body_line_spacing = profile.type_scale.body_line_spacing
    box_width_in = box.width * (profile.canvas_width_emu / EMU_PER_INCH)
    box_height_in = box.height * (profile.canvas_height_emu / EMU_PER_INCH)
    col_widths_in = [box_width_in * share for share in column_shares(all_rows)]

    size_pt, row_heights_in = _fit_font_size(
        all_rows, family, body_line_spacing, col_widths_in, box_height_in, profile, floor_pt=floor_pt,
    )

    header_fill_hex = header_fill(profile)
    header_text_hex = best_contrast_text_color(header_fill_hex, profile.palette_roles)
    # Тело таблицы — заливкой того же "полюса" светлый/тёмный, что и
    # фактический фон СЛАЙДА под ней (см. докстроку `colorpick.
    # surface_pair_for_luminance` — Task 10 отчёт, находка №3: слепой
    # `palette_roles["surface"]` у VK Tech чёрный, потому что бОльшая часть
    # шаблона тёмная, а демо-раскладка Task 10 светлая — таблица чёрной
    # плашкой на бледном фоне выглядела вырезанной из другого файла).
    body_fill_hex, body_text_hex = surface_pair_for_luminance(
        slide_background_luminance(slide, profile), profile.palette_roles,
    )

    align = spec.align or ["l"] * n_cols
    for col in range(n_cols):
        table.columns[col].width = Emu(round(col_widths_in[col] * EMU_PER_INCH))

    for row_idx, row_values in enumerate(all_rows):
        table.rows[row_idx].height = Emu(max(1, round(row_heights_in[row_idx] * EMU_PER_INCH)))
        is_header = row_idx == 0
        fill_hex = header_fill_hex if is_header else body_fill_hex
        text_hex = header_text_hex if is_header else body_text_hex
        for col_idx in range(n_cols):
            value = row_values[col_idx] if col_idx < len(row_values) else ""
            cell = table.cell(row_idx, col_idx)
            cell.text = str(value)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(fill_hex)
            cell.margin_top = cell.margin_bottom = Emu(round(_CELL_VPAD_IN * EMU_PER_INCH))
            paragraph = cell.text_frame.paragraphs[0]
            paragraph.alignment = _ALIGN_MAP.get(align[col_idx] if col_idx < len(align) else "l", PP_ALIGN.LEFT)
            for run in paragraph.runs:
                run.font.size = Pt(size_pt)
                run.font.name = family
                run.font.bold = is_header and profile.type_scale.bold_is_idiomatic
                run.font.color.rgb = _rgb(text_hex)

    # Рамка по сумме строк, а не по коробке слота: строки короче коробки
    # оставили бы у рамки пустой хвост, и аудит (L02, D05) считал бы
    # площадь, которой на слайде нет.
    frame.height = Emu(sum(table.rows[i].height for i in range(n_rows)))
    return frame


def header_fill(profile: TemplateProfile) -> str:
    """Заливка шапки таблицы: accent1 темы, затем роли `brand`/`accent`.

    Роли палитры назначает модель, и от прогона к прогону по-разному: на
    VK Education `accent` бывал то `#3782BA`, то розовым `#FF3885` (accent2
    темы, цвет точечных выделений), и шапка выходила розовой (прогон 26
    сентября 2026); без модели запасная разметка ставит в `brand` бледный
    `#A0DFE0`. accent1 темы от модели не зависит, им же PowerPoint красит
    шапки встроенных стилей таблиц, и на всех трёх шаблонах VK он совпадает
    с цветом бренда (`#0077FF`), которым шаблон заливает свои таблицы."""
    scheme = profile.theme.scheme if profile.theme is not None else {}
    return (
        scheme.get("accent1") or profile.palette_roles.get("brand")
        or profile.palette_roles.get("accent") or "#000000"
    )


def column_shares(rows: list[list[str]]) -> list[float]:
    """Доли ширины столбцов по содержанию: столбец с длинными словами и
    фразами шире столбца с числами. Равные доли рвали слова по буквам
    («Показа/тель», «Сквозн/ая») в первом столбце, пока соседние с «31,5 ч»
    стояли полупустыми (прогон 26 сентября 2026). Вес столбца: самое
    длинное слово или половина самой длинной ячейки, что больше, плюс
    запас на поля ячейки; считается в символах, точность замера здесь не
    нужна, нужна пропорция."""
    n_cols = max((len(r) for r in rows), default=0)
    weights = []
    for col in range(n_cols):
        cells = [str(r[col]) if col < len(r) else "" for r in rows]
        longest_word = max((len(w) for c in cells for w in c.split()), default=0)
        longest_cell = max((len(c) for c in cells), default=0)
        weights.append(max(longest_word, 0.5 * longest_cell) + 2)
    total = sum(weights)
    return [w / total for w in weights] if total else []


def _fit_font_size(
    rows: list[list[str]], family: str, line_spacing: float, col_widths_in: list[float],
    box_height_in: float, profile: TemplateProfile, *, floor_pt: float | None = None,
) -> tuple[float, list[float]]:
    """Кегль и высоты строк, подобранные циклом вниз от `body` до тех пор,
    пока сумма замеренных высот строк не влезет в `box_height_in`, но не
    ниже `FONT_FLOOR_RATIO * caption` (см. докстроку модуля) или
    `floor_pt`, если он задан."""
    # `type_scale.steps` нормирован к эталонному холсту 13.333″ —
    # `type_scale_pt` денормирует ОБА кегля к РЕАЛЬНОМУ холсту профиля ОДНИМ
    # и тем же коэффициентом (см. `TemplateProfile.denorm_pt`), поэтому
    # соотношение floor/base — и с ним поведение цикла ужимания ниже —
    # не меняется, меняется только абсолютный масштаб (Task 10 отчёт,
    # находка аудита T02: без денормировки тело таблицы на VK Tech выходило
    # завышенным на треть).
    base = profile.type_scale_pt("body", 18.0)
    floor = floor_pt if floor_pt is not None else profile.type_scale_pt("caption", 12.0) * FONT_FLOOR_RATIO

    size = base
    heights = _measure_row_heights(rows, family, size, line_spacing, col_widths_in)
    while sum(heights) > box_height_in and size > floor:
        size = max(size * _SHRINK_FACTOR, floor)
        heights = _measure_row_heights(rows, family, size, line_spacing, col_widths_in)
    return size, heights


def _measure_row_heights(
    rows: list[list[str]], family: str, size_pt: float, line_spacing: float, col_widths_in: list[float],
) -> list[float]:
    heights: list[float] = []
    for row in rows:
        tallest = 0.0
        for col, cell_text in enumerate(row):
            # Текст переносится по ширине столбца ЗА ВЫЧЕТОМ боковых полей
            # ячейки (по умолчанию 0.1″ с каждой стороны): без них замер
            # обещал строку там, где редактор делал две.
            width = max(col_widths_in[min(col, len(col_widths_in) - 1)] - 2 * _CELL_HPAD_IN, 0.1)
            metrics = measure(str(cell_text), family, size_pt, width, line_spacing=line_spacing)
            tallest = max(tallest, metrics.height_in)
        heights.append(tallest + 2 * _CELL_VPAD_IN)
    return heights


def _neutralise_style_banding(table) -> None:
    """Отключает автоматическое чередование/выделение первой строки
    дефолтного `tableStyleId`, которое `python-pptx` пишет в `a:tblPr` при
    `add_table` (см. докстроку модуля — у части шаблонов своего стиля нет
    вовсе, у части он не подходящий; явные заливки ячеек ниже перекрывают
    стиль визуально, но `firstRow`/`bandRow` дополнительно гасят его
    собственные автоматические акценты там, где прямое форматирование по
    какой-то причине не долетело бы, например при повторном открытии файла
    в редакторе, который считает direct formatting не таким приоритетным)."""
    tbl_pr = table._tbl.find(qn("a:tblPr"))
    if tbl_pr is not None:
        tbl_pr.set("firstRow", "0")
        tbl_pr.set("bandRow", "0")


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color.lstrip("#").upper())


def _emu_box(box: Box, profile: TemplateProfile) -> tuple[int, int, int, int]:
    return (
        round(box.left * profile.canvas_width_emu),
        round(box.top * profile.canvas_height_emu),
        round(box.width * profile.canvas_width_emu),
        round(box.height * profile.canvas_height_emu),
    )
