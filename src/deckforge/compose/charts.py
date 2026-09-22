"""Нативные графики поверх `python-pptx` (`GraphicFrame` с `c:chart`).

ТЗ п.2.3 требует графики нативными объектами внутри слайда, не растром —
`slide.shapes.add_chart` даёт ровно это: PowerPoint/LibreOffice открывают
результат как редактируемую диаграмму со своими данными, не картинкой.

## Почему цвет каждого ряда и каждой точки задаётся явно

Без явной раскраски `python-pptx` не трогает `c:dPt`/`c:spPr` серии вовсе —
рендерер (PowerPoint/LibreOffice) красит по цепочке `c:chart` → тема файла
(`accent1..accent6` из `a:clrScheme`), не по дизайн-системе шаблона. На
Google-экспортированных шаблонах эта тема часто заглушка (см.
`ThemeInfo.is_stock_office_palette`) — график выходит в стоковых цветах
Office, будто вставлен из чужого шаблона. `_series_palette` ниже всегда
назначает `profile.chart_series` (собран в `template/chart_palette.py` из
цветных токенов шаблона по весу, достроен оттенками `brand`) явно на каждый
`c:ser`/`c:dPt`.

## Почему у одного ряда красятся точки, а не ряд

Столбчатый/секторный график с ОДНИМ рядом данных, окрашенным целиком одним
цветом, визуально неотличим от ряда одноцветных прямоугольников — сравнение
величин по цвету теряется (собственно то, ради чего вообще нужен цвет на
графике с одной переменной). Поэтому для одиночного ряда категориальных
типов (`bar`/`bar_stacked`/`bar_h`) и всегда для круговых (`pie`/
`doughnut`, где "ряд" по природе формата один, а различать нужно именно
СЕКТОРА) красится каждая ТОЧКА (`c:dPt`), не серия целиком; `highlight_index`
даёт точке акцентный цвет на фоне остальных — тем же приёмом, что шаблоны
почти не пользуются жирным начертанием (4 run'а из 744 у VK Tech, бриф) и
выделяют цветом/кеглем, а не насыщенностью.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_TICK_LABEL_POSITION
from pptx.util import Pt

from deckforge.compose.colorpick import best_contrast_text_color_for_luminance, slide_background_luminance
from deckforge.ooxml.geometry import Box
from deckforge.template.profile import TemplateProfile

EMU_PER_INCH = 914400

_KIND_TO_XL = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "bar_stacked": XL_CHART_TYPE.COLUMN_STACKED,
    "bar_h": XL_CHART_TYPE.BAR_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "area": XL_CHART_TYPE.AREA,
    "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
    "scatter": XL_CHART_TYPE.XY_SCATTER,
}
CHART_KINDS = tuple(_KIND_TO_XL)

# Типы, у которых различать нужно ТОЧКИ ряда, а не сами ряды — категориальные
# столбцы (одна категория = один смысл сравнения) и круговые (см. докстроку
# модуля). bar_h входит наравне с bar — тот же случай, повёрнутый на 90°.
_POINT_COLORED_KINDS = frozenset({"bar", "bar_stacked", "bar_h", "pie", "doughnut"})

# Типы, у которых есть подписываемые оси — круговые исключены (сектор, не
# ось, несёт категорию). `scatter` — на первый взгляд тоже кандидат на
# исключение (в OOXML у него `c:valAx` по обеим осям, не `c:catAx`), но
# `chart.category_axis` в python-pptx отдаёт первую из осей вне зависимости
# от типа (для scatter это `ValueAxis`, а не `CategoryAxis`) и несёт тот же
# набор свойств (`has_title`/`axis_title`/`tick_labels`) — подписи осей и
# единиц нужны точечному графику ничуть не меньше столбчатого (бриф,
# проверка аудита I05), исключать его было бы произвольным упрощением, а не
# ограничением формата.
_CATEGORY_AXIS_KINDS = frozenset({"bar", "bar_stacked", "bar_h", "line", "area", "scatter"})


@dataclass(frozen=True)
class Series:
    name: str
    values: list[float]


@dataclass(frozen=True)
class ChartSpec:
    kind: str
    categories: list[str]
    series: list[Series]
    unit: str | None = None
    highlight_index: int | None = None
    # (подпись оси категорий, подпись оси значений) — бриф, проверка аудита
    # I05 "у диаграммы нет подписей осей, единиц или легенды": обе подписи
    # обязательны для смысловых графиков, `None` допускается только там, где
    # осей нет вовсе (pie/doughnut).
    axis_titles: tuple[str, str] | None = None


def add_chart(slide, box: Box, spec: ChartSpec, profile: TemplateProfile):
    """Строит нативный график в `box` (доли холста) на `slide`. Возвращает
    `GraphicFrame` (`.chart`, `.has_chart` — интерфейс `python-pptx`)."""
    if spec.kind not in _KIND_TO_XL:
        raise ValueError(f"Неизвестный тип графика {spec.kind!r} — ожидается один из {CHART_KINDS}")
    if not spec.series:
        raise ValueError("ChartSpec.series пуст — графику нечего рисовать")

    left, top, width, height = _emu_box(box, profile)
    palette = _series_palette(profile, len(spec.series))

    if spec.kind == "scatter":
        frame = _add_scatter(slide, left, top, width, height, spec)
    else:
        chart_data = CategoryChartData()
        chart_data.categories = spec.categories
        for series in spec.series:
            chart_data.add_series(series.name, series.values)
        frame = slide.shapes.add_chart(_KIND_TO_XL[spec.kind], left, top, width, height, chart_data)

    _style_chart(slide, frame.chart, spec, profile, palette)
    return frame


def _add_scatter(slide, left, top, width, height, spec: ChartSpec):
    chart_data = XyChartData()
    x_values = _numeric_categories(spec.categories)
    for series in spec.series:
        xy_series = chart_data.add_series(series.name)
        for x, y in zip(x_values, series.values):
            xy_series.add_data_point(x, y)
    return slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, left, top, width, height, chart_data)


def _numeric_categories(categories: list[str]) -> list[float]:
    """X-значения точечного графика: сами категории, если они и правда
    числа (частый случай — временной/числовой ряд), иначе порядковый номер
    1..N — scatter не умеет рисовать нечисловую ось X, а порядковый номер
    хотя бы сохраняет заявленный порядок категорий."""
    try:
        return [float(c) for c in categories]
    except (TypeError, ValueError):
        return [float(i + 1) for i in range(len(categories))]


def _style_chart(slide, chart, spec: ChartSpec, profile: TemplateProfile, palette: list[str]) -> None:
    family = profile.type_scale.families[0] if profile.type_scale.families else "Arial"
    size_pt = profile.type_scale.steps.get("caption", 12.0)
    # Текст графика (шрифт/подписи осей) рисуется прямо на фоне СЛАЙДА — у
    # диаграммы нет собственной непрозрачной заливки под текстом, в отличие
    # от ячейки таблицы/карточки схемы. Фиксированная роль "on_surface" не
    # годится вслепую (см. докстроку colorpick.py, "Task 10 отчёт, находка
    # №1"): цвет подбирается по фактической яркости фона МАКЕТА, на который
    # положен `slide`.
    bg_luminance = slide_background_luminance(slide, profile)
    text_hex = best_contrast_text_color_for_luminance(bg_luminance, profile.palette_roles)

    chart.font.name = family
    chart.font.size = Pt(size_pt)
    chart.font.color.rgb = _rgb(text_hex)

    _color_series(chart, spec, palette)
    _apply_legend(chart, spec)
    if spec.kind in _CATEGORY_AXIS_KINDS:
        _apply_axes(chart, spec, family, size_pt, text_hex)


def _color_series(chart, spec: ChartSpec, palette: list[str]) -> None:
    """Task 10 отчёт (визуальное ревью, находка №2): у pie/doughnut КАЖДАЯ
    точка — своя КАТЕГОРИЯ (не повтор одного и того же ряда, как у
    одиночного bar), поэтому раскраска "база для всех + акцент для
    highlight_index" здесь неверна — на живом рендере VK Tech три из четырёх
    секторов вышли ОДНИМ и тем же цветом (слились в одну фигуру визуально),
    и только помеченный highlight_index сектор отличался. Круговые красятся
    ПОЛНЫМ циклом палитры по точкам; "база+акцент" остаётся только для
    единственного ряда столбцов (`bar`/`bar_stacked`/`bar_h`), где все точки
    и так один и тот же показатель — там разный цвет как раз обязан
    появляться ТОЛЬКО у выделенной точки, см. `test_single_series_colors_
    points_not_series`."""
    plot = chart.plots[0]
    is_pie_like = spec.kind in ("pie", "doughnut")
    single_series_bars = spec.kind in _POINT_COLORED_KINDS and not is_pie_like and len(spec.series) == 1

    for series_index, series_obj in enumerate(plot.series):
        if is_pie_like:
            # `highlight_index` у pie/doughnut сознательно ИГНОРИРУЕТСЯ (Task 10
            # отчёт, повторное визуальное ревью, находка №7): каждая точка и так
            # получает свой цвет циклом палитры — сдвиг цвета выделенной точки
            # на palette[i+1] реально СХЛОПЫВАЛ две разные категории в один hex,
            # когда сосед по кругу и так нёс этот цвет по своей естественной
            # позиции (живой замер, Education, 4 категории — выделенная "Мар" и
            # соседняя "Апр" получили одинаковый #CBEDEE).
            for point_index, point in enumerate(series_obj.points):
                _fill(point, palette[point_index % len(palette)])
        elif single_series_bars:
            base_hex = palette[series_index % len(palette)]
            highlight_hex = palette[(series_index + 1) % len(palette)] if len(palette) > 1 else base_hex
            for point_index, point in enumerate(series_obj.points):
                color_hex = (
                    highlight_hex
                    if spec.highlight_index is not None and point_index == spec.highlight_index
                    else base_hex
                )
                _fill(point, color_hex)
        else:
            color_hex = palette[series_index % len(palette)]
            _fill(series_obj, color_hex)
            if spec.kind == "line":
                # Линия без заливки области — цвет самой линии, а не только
                # маркеров, иначе явная раскраска не видна на графике вовсе.
                series_obj.format.line.color.rgb = _rgb(color_hex)


def _fill(target, color_hex: str) -> None:
    target.format.fill.solid()
    target.format.fill.fore_color.rgb = _rgb(color_hex)


def _apply_legend(chart, spec: ChartSpec) -> None:
    """Легенда появляется, только когда она РАЗЛИЧАЕТ что-то на графике:
    у столбцов/линий/площадей это число РЯДОВ (один ряд — нечего подписывать
    легендой, категории и так на оси), у круговых — число КАТЕГОРИЙ (ряд
    там всегда один по природе формата, легенда подписывает сектора)."""
    if spec.kind in ("pie", "doughnut"):
        has_legend = len(spec.categories) > 1
    else:
        has_legend = len(spec.series) > 1
    chart.has_legend = has_legend
    if has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False


def _apply_axes(chart, spec: ChartSpec, family: str, size_pt: float, text_hex: str) -> None:
    category_axis = chart.category_axis
    value_axis = chart.value_axis

    if spec.axis_titles:
        category_title, value_title = spec.axis_titles
        if category_title:
            category_axis.has_title = True
            category_axis.axis_title.text_frame.text = category_title
            _color_title_runs(category_axis.axis_title, family, size_pt, text_hex)
        value_axis.has_title = True
        value_axis.axis_title.text_frame.text = (
            f"{value_title}, {spec.unit}" if value_title and spec.unit else (value_title or spec.unit or "")
        )
        _color_title_runs(value_axis.axis_title, family, size_pt, text_hex)
    elif spec.unit:
        value_axis.has_title = True
        value_axis.axis_title.text_frame.text = spec.unit
        _color_title_runs(value_axis.axis_title, family, size_pt, text_hex)

    # Отрицательные значения: категория 0 больше не лежит у нижнего края
    # рамки графика, и подписи категорий, оставленные "рядом с осью"
    # (NEXT_TO_AXIS — дефолт), рисуются ПОСРЕДИ столбцов, там, где проходит
    # нулевая линия — нечитаемо. LOW прижимает подписи категорий к
    # физическому низу графика независимо от того, где проходит ось.
    has_negative = any(v < 0 for series in spec.series for v in series.values)
    category_axis.tick_label_position = (
        XL_TICK_LABEL_POSITION.LOW if has_negative else XL_TICK_LABEL_POSITION.NEXT_TO_AXIS
    )

    for axis in (category_axis, value_axis):
        axis.tick_labels.font.size = Pt(size_pt)
        axis.tick_labels.font.name = family
        axis.tick_labels.font.color.rgb = _rgb(text_hex)


def _color_title_runs(axis_title, family: str, size_pt: float, text_hex: str) -> None:
    """Явная раскраска runs подписи оси — не полагается на наследование от
    `chart.font` (Task 10 отчёт, находка №1: на живом рендере подпись оси и
    подписи делений визуально разошлись по цвету, хотя обеим назначался один
    и тот же `text_hex`, — LibreOffice не всегда наследует цвет заголовка
    оси от общего шрифта диаграммы одинаково с подписями делений)."""
    for paragraph in axis_title.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(size_pt)
            run.font.name = family
            run.font.color.rgb = _rgb(text_hex)


def _series_palette(profile: TemplateProfile, n: int) -> list[str]:
    """`profile.chart_series`, зациклённый на случай `n` рядов больше
    длины палитры (не должно происходить в пределах `MAX_CHART_SERIES`
    (`template/chart_palette.py`), но график, собранный руками мимо
    подборщика паттернов, может запросить сколько угодно рядов — честный
    повтор цветов лучше `IndexError`)."""
    palette = list(profile.chart_series)
    if not palette:
        palette = [profile.palette_roles.get("brand", "#3366CC")]
    if len(palette) >= n:
        return palette
    return [palette[i % len(palette)] for i in range(n)]


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color.lstrip("#").upper())


def _emu_box(box: Box, profile: TemplateProfile) -> tuple[int, int, int, int]:
    return (
        round(box.left * profile.canvas_width_emu),
        round(box.top * profile.canvas_height_emu),
        round(box.width * profile.canvas_width_emu),
        round(box.height * profile.canvas_height_emu),
    )
