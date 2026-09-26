"""Типы числовых данных источников и решение, как их показать, до письма.

Раньше решение «график или список» принимал писатель, и не принимал: ни в
одной колоде живых прогонов до задачи V1 графика не было, хотя в
источниках лежали таблицы по кварталам. Теперь решает этот слой, по
правилам, без модели: каждая таблица и каждый список «подпись: число»
получает тип, тип даёт `VisualIntent` (что показать, обязательно ли и
почему), и структура, контракт и писатель получают его готовым. Писатель
только подписывает и формулирует вывод.

Типы и правила:
- `TimeSeries`: категории время (кварталы, годы, месяцы). От
  `MIN_TIME_POINTS` точек график обязателен: столбики, у длинного ряда
  линия;
- `CategoryComparison`: категории не время. От `MIN_COMPARE_CATEGORIES`
  график-столбики обязателен;
- `PartToWhole`: части складываются в итог таблицы (строка «Итого») или
  в 100%. Накопительные столбики, у одного ряда из пяти частей и меньше
  круговая; обязателен от трёх частей;
- `MetricSet`: одно-два числа. Показатель (`kpi`), не обязателен:
  подходящий пункт структуры есть не всегда;
- `TableData`: единицы в столбце разные («31,5 ч», «39%») или матрица
  больше графика. Таблица, не обязательна."""
from __future__ import annotations
from dataclasses import dataclass, field

from deckforge.plan.series import (
    MAX_SERIES, NumericSeries, RawTable, find_tables, numeric_columns, numeric_series_of, parse_number, stems,
)

DATA_TYPES = ("TimeSeries", "CategoryComparison", "PartToWhole", "MetricSet", "TableData")

MIN_TIME_POINTS = 4
MIN_COMPARE_CATEGORIES = 3
MIN_PARTS = 3
# Длиннее: линия читается лучше столбиков.
LINE_MIN_POINTS = 7
# Строк тела больше: график превращается в гребёнку, нужна таблица.
MAX_CHART_ROWS = 8
# Подпись категории длиннее: столбики лучше положить набок.
LONG_LABEL = 18
# Допуск, с которым части сходятся с итогом (округление в источнике).
_SUM_TOLERANCE = 0.02


@dataclass(frozen=True)
class DataSet:
    """Одна таблица источника с её типом. `ref`: имя для ссылки из
    структуры и контракта («d1»)."""
    ref: str
    type: str
    table: RawTable
    series: NumericSeries | None = None

    @property
    def context(self) -> str:
        return " ".join([self.table.heading, *self.table.header]).strip()

    @property
    def words(self) -> set[str]:
        cats = [r[0] for r in self.table.body if r]
        return stems(self.context + " " + " ".join(cats))


@dataclass(frozen=True)
class VisualIntent:
    """Что показать и почему. `type`: chart / kpi / table. `required`:
    структура обязана отдать под это слайд. `chart_kind`: вид графика по
    правилам типа (`compose.charts.CHART_KINDS`)."""
    type: str
    required: bool
    reason: str
    data_ref: str
    chart_kind: str | None = None
    data: DataSet | None = field(default=None, compare=False)

    def payload(self) -> dict | None:
        """Данные для контракта писателя: готовый ряд графика или строки
        таблицы, как в источнике."""
        if self.data is None:
            return None
        if self.type == "chart" and self.data.series is not None:
            s = self.data.series
            categories = list(s.categories)
            series = [{"name": x.name, "values": list(x.values)} for x in s.series]
            category_title = s.category_title
            if self.chart_kind == "bar_stacked":
                # Части целого стоят строками («Производство контента»,
                # «Платформа»), целое столбцом («2025», «2026»): столбик
                # накопления должен быть целым, иначе складываются годы.
                categories = [x.name for x in s.series]
                series = [
                    {"name": cat, "values": [x.values[i] for x in s.series]}
                    for i, cat in enumerate(s.categories)
                ]
                category_title = ""
            out = {"kind": self.chart_kind or "bar", "categories": categories, "series": series}
            if s.unit:
                out["unit"] = s.unit
            if category_title:
                out["category_title"] = category_title
            return out
        if self.type == "table":
            return {"rows": [list(self.data.table.header), *[list(r) for r in self.data.table.rows]]}
        return None

    def to_dict(self) -> dict:
        out = {"type": self.type, "required": self.required, "reason": self.reason, "data_ref": self.data_ref}
        if self.chart_kind:
            out["chart_kind"] = self.chart_kind
        if self.data is not None:
            out["data_type"] = self.data.type
        return out


def _has_numbers(table: RawTable) -> bool:
    return any(parse_number(c) is not None for r in table.body for c in r[1:])


def _sums_to_total(table: RawTable, series: NumericSeries) -> bool:
    """Части сходятся с «Итого» таблицы по каждому ряду, или один ряд
    процентов сходится в 100."""
    totals = table.total_rows
    if totals:
        total = totals[0]
        for col, parsed in numeric_columns(table):
            cell = parse_number(total[col]) if col < len(total) else None
            if cell is None:
                return False
            part_sum = sum(p[0] for p in parsed)
            if abs(part_sum - cell[0]) > _SUM_TOLERANCE * max(abs(cell[0]), 1.0):
                return False
        return True
    if len(series.series) == 1 and (series.unit or "").strip() == "%":
        return abs(sum(series.series[0].values) - 100.0) <= 2.0
    return False


def classify(table: RawTable, ref: str) -> DataSet | None:
    """Тип таблицы или `None`, если чисел в ней нет вовсе."""
    if not _has_numbers(table):
        return None
    series = numeric_series_of(table)
    body = table.body
    numeric = numeric_columns(table)
    if series is None or len(body) > MAX_CHART_ROWS or len(numeric) > MAX_SERIES:
        return DataSet(ref=ref, type="TableData", table=table, series=series)
    if len(numeric) < len(table.header) - 1 and not table.is_list:
        # Часть столбцов с разными единицами: график показал бы не всё,
        # что таблица говорит, и честнее оставить её таблицей.
        return DataSet(ref=ref, type="TableData", table=table, series=series)
    if len(body) <= 2 and len(series.series) == 1:
        return DataSet(ref=ref, type="MetricSet", table=table, series=series)
    if _sums_to_total(table, series):
        return DataSet(ref=ref, type="PartToWhole", table=table, series=series)
    if series.timelike:
        return DataSet(ref=ref, type="TimeSeries", table=table, series=series)
    return DataSet(ref=ref, type="CategoryComparison", table=table, series=series)


def visual_intent_for(ds: DataSet) -> VisualIntent:
    s = ds.series
    n = len(s.categories) if s is not None else 0
    if ds.type == "TimeSeries":
        kind = "line" if n >= LINE_MIN_POINTS else "bar"
        return VisualIntent(
            type="chart", required=n >= MIN_TIME_POINTS, data_ref=ds.ref, chart_kind=kind, data=ds,
            reason=f"динамика по {n} точкам времени: {'обязателен график' if n >= MIN_TIME_POINTS else 'коротко для графика'}",
        )
    if ds.type == "CategoryComparison":
        long_labels = s is not None and max((len(c) for c in s.categories), default=0) > LONG_LABEL
        return VisualIntent(
            type="chart", required=n >= MIN_COMPARE_CATEGORIES, data_ref=ds.ref,
            chart_kind="bar_h" if long_labels else "bar", data=ds,
            reason=f"сравнение {n} категорий",
        )
    if ds.type == "PartToWhole":
        single = s is not None and len(s.series) == 1 and n <= 5
        # Накопительный столбик держит частей не больше, чем рядов на
        # графике (`MAX_SERIES`): дальше легенда длиннее самого столбика,
        # и честнее сравнить части рядом.
        kind = "pie" if single else ("bar_stacked" if n <= MAX_SERIES else "bar")
        return VisualIntent(
            type="chart", required=n >= MIN_PARTS, data_ref=ds.ref, chart_kind=kind,
            data=ds, reason=f"{n} частей одного целого",
        )
    if ds.type == "MetricSet":
        return VisualIntent(type="kpi", required=False, data_ref=ds.ref, data=ds, reason="одно-два числа: показатель")
    return VisualIntent(
        type="table", required=False, data_ref=ds.ref, data=ds,
        reason="единицы в столбцах разные или строк больше, чем читается на графике",
    )


def type_sources(sources) -> list[DataSet]:
    """Типы всех таблиц всех источников (`outline.SourceDoc`) подряд;
    `ref` по порядку: d1, d2, ..."""
    out: list[DataSet] = []
    for doc in sources or []:
        for table in find_tables(getattr(doc, "text", "") or ""):
            ds = classify(table, f"d{len(out) + 1}")
            if ds is not None:
                out.append(ds)
    return out


def visual_intents(sources) -> list[VisualIntent]:
    return [visual_intent_for(ds) for ds in type_sources(sources)]
