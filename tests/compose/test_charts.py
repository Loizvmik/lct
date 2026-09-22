"""Тесты `compose/charts.py` (Task 10) — brief дословно по составу
случаев: все восемь типов графика строятся, цвета рядов/точек берутся из
`profile.chart_series` (не из темы Office), у графика с одним рядом
красятся ТОЧКИ, а не ряд, легенда появляется только когда несёт смысл,
подписи осей+единиц обязательны, отрицательные значения уводят подписи
категорий вниз."""
from __future__ import annotations

import pytest
from pptx.enum.chart import XL_TICK_LABEL_POSITION

from deckforge.compose.charts import CHART_KINDS, ChartSpec, Series, add_chart


def _series_colors(chart) -> list[str]:
    return [str(series.format.fill.fore_color.rgb) for series in chart.plots[0].series]


def _point_colors(series) -> list[str]:
    return [str(point.format.fill.fore_color.rgb) for point in series.points]


def _value_axis_title(chart) -> str:
    return chart.value_axis.axis_title.text_frame.text


def _tick_label_position(axis) -> str:
    return axis.tick_label_position.name


def test_every_chart_kind_builds(new_slide, PROFILE, BOX):
    assert set(CHART_KINDS) == {
        "bar", "bar_stacked", "bar_h", "line", "area", "pie", "doughnut", "scatter",
    }
    for kind in CHART_KINDS:
        frame = add_chart(
            new_slide(), BOX,
            ChartSpec(kind=kind, categories=["A", "B", "C"], series=[Series("Выручка", [1, 2, 3])], unit="млн ₽"),
            PROFILE,
        )
        assert frame.has_chart, kind


def test_series_colors_come_from_the_template_palette(new_slide, PROFILE, BOX):
    """Без явной окраски python-pptx отдаёт раскраску теме файла, и график
    выходит в цветах Office."""
    frame = add_chart(
        new_slide(), BOX,
        ChartSpec(kind="bar", categories=["A", "B"], series=[Series("s1", [1, 2]), Series("s2", [2, 1])]),
        PROFILE,
    )
    used = _series_colors(frame.chart)
    allowed = {c.lstrip("#").upper() for c in PROFILE.chart_series}
    assert set(used) <= allowed
    assert used  # хоть что-то покрашено


def test_single_series_colors_points_not_series(new_slide, PROFILE, BOX):
    """У одного ряда окраска ряда даёт одинаковые столбцы; красить надо точки."""
    frame = add_chart(
        new_slide(), BOX,
        ChartSpec(kind="bar", categories=["A", "B", "C"], series=[Series("s", [1, 2, 3])], highlight_index=1),
        PROFILE,
    )
    colors = _point_colors(frame.chart.plots[0].series[0])
    assert len(colors) == 3
    assert len(set(colors)) == 2


def test_chart_has_axis_titles_and_units(new_slide, PROFILE, BOX):
    """Проверка аудита I05: у диаграммы нет подписей осей, единиц или легенды."""
    frame = add_chart(
        new_slide(), BOX,
        ChartSpec(
            kind="line", categories=["Янв", "Фев"], series=[Series("Заявки", [10, 12])],
            unit="штук", axis_titles=("Месяц", "Заявки"),
        ),
        PROFILE,
    )
    assert _value_axis_title(frame.chart) == "Заявки, штук"
    assert frame.chart.category_axis.axis_title.text_frame.text == "Месяц"


def test_legend_appears_only_when_it_carries_information(new_slide, PROFILE, BOX):
    one = add_chart(new_slide(), BOX, ChartSpec(kind="bar", categories=["A"], series=[Series("s", [1])]), PROFILE)
    two = add_chart(
        new_slide(), BOX,
        ChartSpec(kind="bar", categories=["A"], series=[Series("s1", [1]), Series("s2", [2])]),
        PROFILE,
    )
    assert one.chart.has_legend is False
    assert two.chart.has_legend is True


def test_pie_legend_reflects_categories_not_series(new_slide, PROFILE, BOX):
    """У круговой диаграммы ряд всегда один по природе формата — легенду
    включает число КАТЕГОРИЙ (секторов), а не рядов."""
    single_category = add_chart(
        new_slide(), BOX, ChartSpec(kind="pie", categories=["Всё"], series=[Series("s", [1])]), PROFILE,
    )
    many_categories = add_chart(
        new_slide(), BOX,
        ChartSpec(kind="pie", categories=["A", "B", "C"], series=[Series("s", [1, 2, 3])]),
        PROFILE,
    )
    assert single_category.chart.has_legend is False
    assert many_categories.chart.has_legend is True


def test_negative_values_move_category_labels_low(new_slide, PROFILE, BOX):
    frame = add_chart(
        new_slide(), BOX, ChartSpec(kind="bar", categories=["A", "B"], series=[Series("s", [-5, 7])]), PROFILE,
    )
    assert _tick_label_position(frame.chart.category_axis) == "LOW"


def test_positive_values_keep_default_tick_label_position(new_slide, PROFILE, BOX):
    frame = add_chart(
        new_slide(), BOX, ChartSpec(kind="bar", categories=["A", "B"], series=[Series("s", [5, 7])]), PROFILE,
    )
    assert frame.chart.category_axis.tick_label_position != XL_TICK_LABEL_POSITION.LOW


def test_unknown_chart_kind_raises(new_slide, PROFILE, BOX):
    with pytest.raises(ValueError):
        add_chart(new_slide(), BOX, ChartSpec(kind="bogus", categories=["A"], series=[Series("s", [1])]), PROFILE)
