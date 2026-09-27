"""Задача V1: слой типов данных решает, как показать числа источников,
до письма (`plan.data_types`)."""
from __future__ import annotations
from pathlib import Path

from deckforge.plan.data_types import classify, type_sources, visual_intent_for, visual_intents
from deckforge.plan.outline import SourceDoc
from deckforge.plan.series import find_tables, parse_number

PACKS = Path("fixtures/content-packs")


def _sources(pack: str) -> list[SourceDoc]:
    return [SourceDoc(name="sources.md", text=(PACKS / pack / "sources.md").read_text(encoding="utf-8"))]


def _one(text: str):
    tables = find_tables(text)
    assert len(tables) == 1
    return classify(tables[0], "d1")


def test_quarterly_coverage_is_a_time_series_that_requires_a_chart():
    """Таблица охвата по кварталам: четыре точки времени, график
    обязателен, ряды и числа как в источнике."""
    datasets = type_sources(_sources("edu-platform"))
    coverage = next(d for d in datasets if d.table.heading == "Охват")
    assert coverage.type == "TimeSeries"
    vi = visual_intent_for(coverage)
    assert vi.type == "chart" and vi.required and vi.chart_kind == "bar"
    data = vi.payload()
    assert data["categories"] == ["I", "II", "III", "IV"]
    assert [s["name"] for s in data["series"]] == ["Регистраций", "Приступили", "Завершили курс"]
    assert data["series"][0]["values"] == [4100.0, 6300.0, 5800.0, 9400.0]
    assert data["category_title"] == "Квартал"


def test_parts_that_add_up_to_the_total_row_are_part_to_whole_stacked_by_the_whole():
    """«Экономика»: статьи складываются в «Итого» каждого года. Столбик
    накопления строится по году (целому), статьи идут рядами: иначе
    складывались бы годы."""
    datasets = type_sources(_sources("edu-platform"))
    economy = next(d for d in datasets if d.table.heading == "Экономика")
    assert economy.type == "PartToWhole"
    vi = visual_intent_for(economy)
    assert vi.required and vi.chart_kind == "bar_stacked"
    data = vi.payload()
    assert data["categories"] == ["2025", "2026"]
    assert [s["name"] for s in data["series"]] == ["Производство контента", "Платформа и инфраструктура", "Сопровождение"]
    assert data["series"][0]["values"] == [31.0, 44.0]
    assert data["unit"] == "млн ₽"


def test_columns_with_different_quantities_stay_a_table():
    """«31,5 ч» рядом с «39%» и «2,8 балла» в одном столбце: разные
    величины, не разные единицы одной; такие данные остаются таблицей, и
    графика код не требует (длительности в разных единицах приводятся к
    одной, см. тест ниже)."""
    intents = visual_intents(_sources("queue-latency"))
    pilot = [vi for vi in intents if vi.data.table.heading.startswith("Пилот")]
    assert pilot and all(vi.type == "table" and not vi.required for vi in pilot)


def test_category_comparison_from_a_label_value_list():
    ds = _one("## Выручка по регионам\n\n- Москва: 120\n- Казань: 80\n- Томск: 45\n")
    assert ds.type == "CategoryComparison"
    vi = visual_intent_for(ds)
    assert vi.type == "chart" and vi.required and vi.chart_kind == "bar"
    assert vi.payload()["categories"] == ["Москва", "Казань", "Томск"]


def test_two_numbers_are_a_metric_set_not_a_chart():
    ds = _one("| Год | Стоимость выпускника |\n|---|---|\n| 2025 | 14 300 ₽ |\n| 2026 | 7 990 ₽ |\n")
    assert ds.type == "MetricSet"
    vi = visual_intent_for(ds)
    assert vi.type == "kpi" and not vi.required


def test_short_time_series_is_offered_but_not_required():
    ds = _one("| Год | Выпуск |\n|---|---|\n| 2024 | 10 |\n| 2025 | 12 |\n| 2026 | 15 |\n")
    assert ds.type == "TimeSeries"
    vi = visual_intent_for(ds)
    assert vi.type == "chart" and not vi.required


def test_long_time_series_is_a_line():
    rows = "".join(f"| {m} | {i * 3} |\n" for i, m in enumerate(
        ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг"], start=1))
    ds = _one("| Месяц | Заявок |\n|---|---|\n" + rows)
    assert visual_intent_for(ds).chart_kind == "line"


def test_numbers_are_read_as_written_in_russian_sources():
    assert parse_number("4 100") == (4100.0, "")
    assert parse_number("31 млн ₽") == (31.0, "млн ₽")
    assert parse_number("98,5%") == (98.5, "%")
    assert parse_number("−80%") == (-80.0, "%")
    assert parse_number("нет данных") is None
    # Английский разделитель тысяч из Excel: группы ровно по три цифры.
    assert parse_number("1,200") == (1200.0, "")
    assert parse_number("12,345,678 ₽") == (12345678.0, "₽")
    assert parse_number("1,25") == (1.25, "")


def test_text_without_numbers_gives_no_dataset():
    assert type_sources([SourceDoc(name="s", text="| Кто | Что |\n|---|---|\n| А | Б |\n| В | Г |\n| Д | Е |\n")]) == []


def test_durations_in_mixed_units_become_one_series_in_hours():
    """Замер процесса: «12 минут» рядом с «18 часов». Раньше столбец с
    разными единицами оставался таблицей; теперь длительности приводятся к
    часам, и шесть этапов сравниваются столбиками, где ожидание в 18 ч
    видно против работы в 9 минут. Таблица пилота («31,5 ч» рядом с «39%»)
    остаётся таблицей: там разные величины, не разные единицы."""
    intents = visual_intents(_sources("queue-latency"))
    kinds = [(vi.type, vi.required) for vi in intents]
    assert kinds[0] == ("chart", True), kinds
    series = intents[0].data.series
    assert series.unit == "ч"
    assert series.series[0].values[:2] == [0.2, 18.0]
    assert kinds[1] == ("table", False), kinds
