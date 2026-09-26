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
from copy import deepcopy
from dataclasses import dataclass, field

from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_TICK_LABEL_POSITION
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.parts.chart import ChartPart
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


def add_chart(slide, box: Box, spec: ChartSpec, profile: TemplateProfile, prototype=None,
              bg_luminance: float | None = None):
    """Строит нативный график в `box` (доли холста) на `slide`. Возвращает
    `GraphicFrame` (`.chart`, `.has_chart` — интерфейс `python-pptx`).

    `prototype` (задача V1): образец графика шаблона
    (`profile.chart_prototypes`): его палитра заменяет палитру профиля,
    если в ней хотя бы два цвета, а его сетка, подписи значений, зазор и
    перекрытие заменяют правила профиля (`profile.chart_rules`).
    `bg_luminance`: яркость того, что под рамкой (плашка раскладки), если
    вызывающий её знает; иначе берётся фон слайда."""
    if spec.kind not in _KIND_TO_XL:
        raise ValueError(f"Неизвестный тип графика {spec.kind!r} — ожидается один из {CHART_KINDS}")
    if not spec.series:
        raise ValueError("ChartSpec.series пуст — графику нечего рисовать")

    left, top, width, height = _emu_box(box, profile)
    palette = _series_palette(profile, len(spec.series))
    own = list(getattr(prototype, "palette", None) or [])
    if len(own) >= 2:
        # Рядов больше, чем цветов на картинке образца: следом идут цвета
        # профиля, которых в образце нет, иначе два ряда выйдут одним цветом.
        # Сравнение по расстоянию, а не по записи: синий образца #0177FF и
        # синий профиля #0077FF на графике один цвет (живой прогон V1).
        extra = [c for c in palette if all(_rgb_distance(c, o) >= _DISTINCT_RGB for o in own)]
        palette = own + extra
        palette = [palette[i % len(palette)] for i in range(max(len(palette), len(spec.series)))]

    if spec.kind == "scatter":
        frame = _add_scatter(slide, left, top, width, height, spec)
    else:
        chart_data = CategoryChartData()
        chart_data.categories = spec.categories
        for series in spec.series:
            chart_data.add_series(series.name, series.values)
        frame = slide.shapes.add_chart(_KIND_TO_XL[spec.kind], left, top, width, height, chart_data)

    _style_chart(slide, frame.chart, spec, profile, palette, bg_luminance)
    rules = rules_of_prototype(prototype) if prototype is not None else getattr(profile, "chart_rules", None)
    apply_chart_rules(frame.chart, spec, rules)
    if getattr(rules, "data_labels", False):
        _show_values(frame.chart, spec)
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


def _style_chart(
    slide, chart, spec: ChartSpec, profile: TemplateProfile, palette: list[str], bg_luminance: float | None = None,
) -> None:
    family = profile.type_scale.families[0] if profile.type_scale.families else "Arial"
    # `type_scale.steps` нормирован к эталонному холсту 13.333″ —
    # `type_scale_pt` денормирует к РЕАЛЬНОМУ холсту профиля (см. докстроку
    # `TemplateProfile.denorm_pt` — Task 10 отчёт, находка аудита T02: без
    # этого кегль текста графика на VK Tech выходил завышенным на треть).
    size_pt = profile.type_scale_pt("caption", 12.0)
    # Текст графика (шрифт/подписи осей) рисуется прямо на фоне СЛАЙДА — у
    # диаграммы нет собственной непрозрачной заливки под текстом, в отличие
    # от ячейки таблицы/карточки схемы. Фиксированная роль "on_surface" не
    # годится вслепую (см. докстроку colorpick.py, "Task 10 отчёт, находка
    # №1"): цвет подбирается по фактической яркости фона МАКЕТА, на который
    # положен `slide`.
    if bg_luminance is None:
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
            # `highlight_index` у pie/doughnut сознательно ИГНОРИРУЕТСЯ — дефект
            # найден ПОВТОРНЫМ визуальным ревью Task 10, ПОСЛЕ официального
            # списка из семи находок отчёта задачи (не путать с находкой №2
            # выше, "три сектора одним цветом" — про раскраску РЯДА целиком;
            # этот дефект — про то, что даже раскраска ПО ТОЧКАМ ломается,
            # если поверх нее ещё сдвигать цвет выделенной точки): каждая
            # точка и так получает свой цвет циклом палитры — сдвиг цвета
            # выделенной точки на palette[i+1] реально СХЛОПЫВАЛ две разные
            # категории в один hex, когда сосед по кругу и так нёс этот цвет
            # по своей естественной позиции (живой замер, Education, 4
            # категории — выделенная "Мар" и соседняя "Апр" получили
            # одинаковый #CBEDEE).
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


# ---------------------------------------------------------------------------
# Задача V1: правила оформления шаблона и родной график примера
# ---------------------------------------------------------------------------

# Типы, у которых есть `gap_width`/`overlap` (столбики и полосы).
_BAR_KINDS = frozenset({"bar", "bar_stacked", "bar_h"})


def apply_chart_rules(chart, spec: ChartSpec, rules) -> None:
    """Правила оформления диаграмм из текста шаблона (`profile.
    chart_rules`, слайд 51 VK Education): перекрытие рядов, боковой зазор,
    без линий сетки. `rules` пустые или `None`: график остаётся как есть.

    Перекрытие и зазор шаблон описывает для нескольких рядов: у одного
    ряда нулевой боковой зазор склеил бы столбики в сплошной блок, а
    пример самого шаблона (картинка на том же слайде) рисует одиночный
    ряд с просветами. Поэтому у одного ряда эти два правила не
    применяются; сетка убирается всегда, когда шаблон так просит."""
    if rules is None:
        return
    if spec.kind in _BAR_KINDS and len(spec.series) > 1:
        plot = chart.plots[0]
        overlap = getattr(rules, "overlap", None)
        if getattr(rules, "gap_width", None) is not None:
            # Зазор между группами не уже зазора внутри группы: при
            # «перекрытии −50%» и «зазоре 0%» группы сливаются, и по
            # графику не понять, где кончается квартал (VK Education).
            inner = -int(overlap) if overlap is not None and overlap < 0 else 0
            plot.gap_width = max(int(rules.gap_width), inner)
        if getattr(rules, "overlap", None) is not None and spec.kind != "bar_stacked":
            # У накопительных столбиков перекрытие 100 по определению.
            plot.overlap = int(rules.overlap)
    if getattr(rules, "no_gridlines", False) and spec.kind in _CATEGORY_AXIS_KINDS:
        chart.value_axis.has_major_gridlines = False
        chart.value_axis.has_minor_gridlines = False
        chart.category_axis.has_major_gridlines = False


# Цвета ближе этого (евклидово по RGB 0..255) на графике не различить.
_DISTINCT_RGB = 60.0


def _rgb_distance(a: str, b: str) -> float:
    pa = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    pb = [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return sum((x - y) ** 2 for x, y in zip(pa, pb)) ** 0.5


@dataclass(frozen=True)
class _Rules:
    gap_width: int | None = None
    overlap: int | None = None
    no_gridlines: bool = False
    data_labels: bool = False


def rules_of_prototype(prototype) -> _Rules:
    """Правила оформления из образца графика шаблона в том же виде, что
    `profile.chart_rules`."""
    return _Rules(
        gap_width=getattr(prototype, "gap_width", None), overlap=getattr(prototype, "overlap", None),
        no_gridlines=not getattr(prototype, "gridlines", True),
        data_labels=bool(getattr(prototype, "show_values", False)),
    )


def _show_values(chart, spec: ChartSpec) -> None:
    """Подписи значений на столбиках и точках: шаблон просит «метки данных
    вместо вертикальной оси» (VK Education, слайд 51). Ось значений
    остаётся: без неё аудит (I05) не узнает единицу, а у длинного ряда она
    нужна и по правилу самого шаблона."""
    plot = chart.plots[0]
    plot.has_data_labels = True
    labels = plot.data_labels
    labels.show_value = True
    labels.number_format_is_linked = True
    if spec.kind in ("bar", "bar_h"):
        labels.position = XL_LABEL_POSITION.OUTSIDE_END
    elif spec.kind == "line":
        labels.position = XL_LABEL_POSITION.ABOVE


# Круговые типы примера: в них ложится только один неотрицательный ряд.
_PIE_TYPES = frozenset({
    XL_CHART_TYPE.PIE, XL_CHART_TYPE.PIE_EXPLODED, XL_CHART_TYPE.DOUGHNUT, XL_CHART_TYPE.DOUGHNUT_EXPLODED,
})


def native_chart_accepts(chart, spec: ChartSpec) -> bool:
    """Годится ли родной график примера под наши данные с сохранением его
    типа. Круговой пример не покажет несколько рядов, точечный не покажет
    текстовые категории; тогда сборка рисует свой график в его рамке."""
    try:
        chart_type = chart.chart_type
    except Exception:  # noqa: BLE001: незнакомый тип python-pptx не называет
        return False
    if chart_type in _PIE_TYPES:
        return len(spec.series) == 1 and all(v >= 0 for v in spec.series[0].values)
    if chart_type in (XL_CHART_TYPE.XY_SCATTER, XL_CHART_TYPE.BUBBLE):
        return False
    return spec.kind != "scatter"


def fill_native_chart(slide, frame_element, spec: ChartSpec) -> bool:
    """Подменяет данные родного графика примера на клоне: тип, цвета,
    шрифты, легенда и оформление осей остаются дизайнерскими, меняются
    категории, ряды и встроенная таблица Excel. Возвращает `False`, если
    график под эти данные не годится (`native_chart_accepts`): тогда
    вызывающий рисует свой.

    Часть графика у клона общая с примером (`clone._copy_relationships`
    связывает клон с той же частью), и правка на месте поменяла бы данные
    у каждого клона того же примера. Поэтому часть сперва копируется
    (`_detach_chart_part`)."""
    frame = _frame_of(slide, frame_element)
    if frame is None or not getattr(frame, "has_chart", False):
        return False
    if not native_chart_accepts(frame.chart, spec):
        return False
    _detach_chart_part(slide, frame_element)
    chart = _frame_of(slide, frame_element).chart
    own_series = len(list(chart._chartSpace.iter(qn("c:ser"))))  # noqa: SLF001
    data = CategoryChartData()
    data.categories = spec.categories
    for series in spec.series:
        data.add_series(series.name, series.values)
    chart.replace_data(data)
    _color_added_series(chart, own_series)
    _free_value_scale(chart)
    _native_axis_titles(chart, spec)
    return True


def _color_added_series(chart, own_series: int) -> None:
    """Ряды сверх рядов примера python-pptx копирует с последнего, вместе с
    его заливкой, и два соседних ряда выходят одного цвета (ЛЦТ2026: пример
    на два ряда, у нас три). Новому ряду даётся следующий акцент темы, тем
    же способом, каким дизайнер покрасил свои (`a:schemeClr accentN`)."""
    for i, ser in enumerate(chart._chartSpace.iter(qn("c:ser"))):  # noqa: SLF001
        if i < own_series:
            continue
        sp_pr = ser.find(qn("c:spPr"))
        fill = sp_pr.find(qn("a:solidFill")) if sp_pr is not None else None
        if fill is None:
            continue
        for child in list(fill):
            fill.remove(child)
        scheme = fill.makeelement(qn("a:schemeClr"), {"val": f"accent{i % 6 + 1}"})
        fill.append(scheme)


def _frame_of(slide, frame_element):
    return next((sh for sh in slide.shapes if sh._element is frame_element), None)  # noqa: SLF001


def _detach_chart_part(slide, frame_element) -> None:
    """Своя копия части графика для рамки `frame_element`: XML тот же, связи
    со стилем и цветами графика те же (их никто не правит). Ссылка на
    встроенную таблицу убирается, и `replace_data` заводит новую таблицу
    для копии, а таблица примера остаётся нетронутой."""
    chart_ref = frame_element.find(".//" + qn("c:chart"))
    old_rid = chart_ref.get(qn("r:id"))
    old_part = slide.part.related_part(old_rid)
    package = old_part.package
    partname = package.next_partname(ChartPart.partname_template)
    new_part = ChartPart.load(partname, old_part.content_type, package, old_part.blob)
    space = new_part._element  # noqa: SLF001
    for ext in space.findall(qn("c:externalData")):
        space.remove(ext)
    for rel in old_part.rels.values():
        if rel.is_external or rel.reltype == RT.PACKAGE:
            continue
        new_part.relate_to(rel.target_part, rel.reltype)
    chart_ref.set(qn("r:id"), slide.part.relate_to(new_part, RT.CHART))
    slide.part.drop_rel(old_rid)


def _free_value_scale(chart) -> None:
    """Шкала примера рассчитана на его числа: у ЛЦТ2026 шаг оси 1 при
    значениях до 5, и на наших тысячах это тысячи делений. Шаг и границы
    отдаются программе показа (авто)."""
    for val_ax in chart._chartSpace.iter(qn("c:valAx")):  # noqa: SLF001
        for tag in ("c:majorUnit", "c:minorUnit"):
            for el in val_ax.findall(qn(tag)):
                val_ax.remove(el)
        scaling = val_ax.find(qn("c:scaling"))
        if scaling is not None:
            for tag in ("c:max", "c:min"):
                for el in scaling.findall(qn(tag)):
                    scaling.remove(el)


def _native_axis_titles(chart, spec: ChartSpec) -> None:
    """Подписи осей родного графика (I05 аудита требует их у осевых
    типов). Начертание берётся у подписей делений той же оси, чтобы
    подпись не вышла чёрной на тёмном фоне примера."""
    try:
        chart_type = chart.chart_type
    except Exception:  # noqa: BLE001
        return
    if chart_type in _PIE_TYPES or not spec.axis_titles:
        return
    category_title, value_title = spec.axis_titles
    value_text = f"{value_title}, {spec.unit}" if value_title and spec.unit else (value_title or spec.unit or "")
    # Ручная раскладка области построения примера не оставляет места под
    # подписи осей (у ЛЦТ2026 подпись оси значений легла на числа делений):
    # с подписями раскладку считает программа показа.
    plot_area = chart._chartSpace.find(qn("c:chart") + "/" + qn("c:plotArea"))  # noqa: SLF001
    layout = plot_area.find(qn("c:layout")) if plot_area is not None else None
    if layout is not None:
        for child in list(layout):
            layout.remove(child)
    for axis_name, text in (("category_axis", category_title), ("value_axis", value_text)):
        if not text:
            continue
        try:
            axis = getattr(chart, axis_name)
        except Exception:  # noqa: BLE001: у типа нет такой оси
            continue
        axis.has_title = True
        axis.axis_title.text_frame.text = text
        def_rpr = axis._element.find(  # noqa: SLF001
            qn("c:txPr") + "/" + qn("a:p") + "/" + qn("a:pPr") + "/" + qn("a:defRPr"),
        )
        if def_rpr is None:
            continue
        for run in axis.axis_title.text_frame.paragraphs[0].runs:
            r_pr = deepcopy(def_rpr)
            r_pr.tag = qn("a:rPr")
            old = run._r.find(qn("a:rPr"))  # noqa: SLF001
            if old is not None:
                run._r.remove(old)  # noqa: SLF001
            run._r.insert(0, r_pr)  # noqa: SLF001
