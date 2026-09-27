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
- `TableData`: единицы в столбце разные («31,5 ч», «39%»), матрица
  больше графика или в таблице вовсе слова, а не числа. Таблица. Обязательна
  у таблицы сравнения (`is_comparison_table`): столбцы со словами по
  критериям, не по времени. Такая таблица, пересказанная карточками,
  теряет сопоставление строк, ради которого её и писали.

Кроме таблиц слой находит в источниках цитаты (`find_quotes`): абзац в
кавычках и подпись с именем. Цитата не число, но решение то же: показать
её цитатой, а не абзацем, решает код до письма."""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from deckforge.plan.series import (
    MAX_SERIES, NumericSeries, RawTable, find_tables, numeric_columns, numeric_series_of, parse_number, stems,
    timelike_labels,
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
    """Что показать и почему. `type`: chart / kpi / table / quote.
    `required`: структура обязана отдать под это слайд. `chart_kind`: вид
    графика по правилам типа (`compose.charts.CHART_KINDS`). `quote`:
    цитата источника у `type == "quote"`."""
    type: str
    required: bool
    reason: str
    data_ref: str
    chart_kind: str | None = None
    data: DataSet | None = field(default=None, compare=False)
    quote: "SourceQuote | None" = field(default=None, compare=False)

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


def _text_column(table: RawTable, col: int) -> bool:
    """Столбец несёт слова, а не числа: больше половины ячеек тела не
    начинаются с числа («автор заявки», «до 4 часов»)."""
    cells = [r[col] for r in table.body if col < len(r) and r[col].strip()]
    if not cells:
        return False
    words = sum(1 for c in cells if parse_number(c) is None)
    return words * 2 > len(cells)


def is_comparison_table(table: RawTable) -> bool:
    """Таблица сравнения: строки это критерии, а не моменты времени, хоть
    один столбец значений несёт слова, и сравниваемых столбцов не меньше
    двух (две колонки «термин / пояснение» это словарь, не сравнение). План
    по кварталам сюда не входит: у него своя форма (дорожная карта,
    таймлайн), и обязательная таблица отняла бы её у визуального варианта."""
    if table.is_list or len(table.header) < 3 or len(table.body) < 2:
        return False
    if timelike_labels([r[0] for r in table.body if r]):
        return False
    return any(_text_column(table, col) for col in range(1, len(table.header)))


def classify(table: RawTable, ref: str) -> DataSet | None:
    """Тип таблицы или `None`, если в ней нет ни чисел, ни сравнения
    словами."""
    if not _has_numbers(table):
        # Таблица из одних слов («ручная / автоматическая маршрутизация»):
        # раньше пропадала из слоя данных, и писатель пересказывал её
        # карточками мелким шрифтом.
        if is_comparison_table(table):
            return DataSet(ref=ref, type="TableData", table=table)
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
    if is_comparison_table(ds.table):
        return VisualIntent(
            type="table", required=True, data_ref=ds.ref, data=ds,
            reason="сравнение по критериям словами: таблица держит сопоставление строк",
        )
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
    """Визуалы таблиц источников, затем цитаты (`type == "quote"`,
    необязательные: структура их не раздаёт, форму цитаты назначает стиль,
    `pattern.shape`)."""
    out = [visual_intent_for(ds) for ds in type_sources(sources)]
    for i, quote in enumerate(source_quotes(sources), start=1):
        out.append(VisualIntent(
            type="quote", required=False, data_ref=f"q{i}", quote=quote,
            reason="цитата в источнике: абзац в кавычках и подпись с именем",
        ))
    return out


@dataclass(frozen=True)
class SourceQuote:
    """Цитата источника дословно и её автор («Сергей Лебедев, директор по
    операциям»)."""
    text: str
    author: str

    @property
    def words(self) -> set[str]:
        return stems(self.text + " " + self.author)


# Абзац в кавычках с начала строки, за ним подпись: имя из двух-трёх слов
# с заглавной и, через запятую, должность. Имя обязательно: кавычки без
# автора в источниках чаще название продукта или термин, а не цитата.
_QUOTE_RE = re.compile(
    r"(?:^|\n)[ \t]*[«“\"](?P<text>[^«»“”\"]{20,600}?)[»”\"][ \t]*\n?[ \t]*[—–-]?[ \t]*"
    r"(?P<name>[А-ЯЁA-Z][а-яёa-z]+(?:[ \t]+[А-ЯЁA-Z][а-яёa-z.]+){1,2})"
    r"(?:[ \t]*,[ \t]*(?P<role>[^\n]{2,80}))?",
)


def find_quotes(text: str) -> list[SourceQuote]:
    out = []
    for m in _QUOTE_RE.finditer(text or ""):
        body = " ".join(m.group("text").split())
        author = m.group("name").strip()
        role = (m.group("role") or "").strip().rstrip(".;")
        out.append(SourceQuote(text=body, author=f"{author}, {role}" if role else author))
    return out


def source_quotes(sources) -> list[SourceQuote]:
    return [q for doc in sources or [] for q in find_quotes(getattr(doc, "text", "") or "")]
