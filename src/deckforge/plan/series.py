"""Разбор чисел в исходных материалах: markdown-таблицы и списки
«подпись: число» как они есть, и числовые ряды, пригодные для графика.

Что с ними делать (график, показатель, таблица), решает не этот модуль, а
слой типов данных (`plan.data_types`); здесь только чтение текста. Числа
берутся из источника как написаны: писатель потом сверяет их с тем же
источником (`check_number`).

Строка «Итого/Всего» в ряд не входит: сумма рядом с частями сплющивает
остальные столбики и сравнения не несёт. Столбец, где единицы разные
(«12 минут» рядом с «18 часов»), рядом не считается: на графике два
столбика вышли бы почти равными при разнице в девяносто раз."""
from __future__ import annotations
import re
from dataclasses import dataclass, field

MIN_POINTS = 3
# Рядов на одном графике не больше этого: дальше легенда читается хуже
# самого графика (тот же порог, что у контракта, `max_series`).
MAX_SERIES = 4
MAX_POINTS = 12

_TOTAL_RE = re.compile(r"^\s*(итого|всего|сумма|total)\b", re.IGNORECASE)
# Число в начале ячейки: знак, цифры с пробелами-разделителями тысяч,
# дробная часть через запятую или точку. Хвост после числа: единица.
_NUMBER_RE = re.compile(r"^\s*([+\-−]?\d[\d\s  ]*(?:,\d{3})*(?:[.,]\d+)?)\s*(.*?)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_POINT_RE = re.compile(
    r"^\s*(?:[-*•]\s+)?([^:|]{1,40}?)\s*[:—–]\s*([+\-−]?\d[\d\s  ]*(?:[.,]\d+)?)\s*([^\s,;.]*)\s*[,;.]?\s*$",
)
# Категории, которые читаются как время: кварталы, годы, месяцы, даты.
_TIME_RE = re.compile(
    r"^(?:[IVX]{1,4}(?:\s*кв\w*)?|Q[1-4]|[1-4]\s*кв\w*|(?:19|20)\d\d|янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт"
    r"|ноя|дек|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}[./]\d{2,4})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RawTable:
    """Таблица или список «подпись: число» как в источнике. У списка шапка
    из двух колонок: раздел и «Значение». `heading`: заголовок раздела над
    таблицей, по нему структура узнаёт, о чём таблица."""
    heading: str
    header: list[str]
    rows: list[list[str]]
    is_list: bool = False

    @property
    def body(self) -> list[list[str]]:
        """Строки без «Итого»."""
        return [r for r in self.rows if r and not _TOTAL_RE.match(r[0])]

    @property
    def total_rows(self) -> list[list[str]]:
        return [r for r in self.rows if r and _TOTAL_RE.match(r[0])]


@dataclass(frozen=True)
class SeriesData:
    name: str
    values: list[float]


@dataclass(frozen=True)
class NumericSeries:
    """Ряд из источника. `context`: заголовок раздела и шапка таблицы,
    по ним структура сопоставляет ряд со своим пунктом. `raw`: те же
    числа, как они написаны в источнике («4 100»), для сверки."""
    categories: list[str]
    series: list[SeriesData]
    unit: str | None = None
    category_title: str = ""
    context: str = ""
    raw: list[str] = field(default_factory=list)

    @property
    def timelike(self) -> bool:
        return all(_TIME_RE.match(c.strip()) for c in self.categories)


_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+$")


def parse_number(cell: str) -> tuple[float, str] | None:
    """(число, хвост-единица) или `None`, если ячейка не число."""
    m = _NUMBER_RE.match(cell or "")
    if not m:
        return None
    digits = re.sub(r"[\s  ]", "", m.group(1)).replace("−", "-")
    # Запятая в русском тексте десятичная («31,5»), но группы ровно по три
    # цифры после неё («1,200», «12,345,678») это английский разделитель
    # тысяч из Excel и аналитики: десятичная часть на три нуля по-русски
    # пишется «1,2». Ревью 27 сентября 2026: «1,200» читалось как 1,2, и
    # сверка с источником этого не ловила, потому что сравнивала так же.
    if _THOUSANDS_RE.match(digits):
        digits = digits.replace(",", "")
    digits = digits.replace(",", ".")
    try:
        return float(digits), m.group(2).strip()
    except ValueError:
        return None


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", line))


def _common_unit(units: list[str]) -> str | None:
    units = [u for u in units if u]
    if not units:
        return None
    first = units[0]
    return first if all(u == first for u in units) else None


def _unit_stem(unit: str) -> str:
    return unit.strip().lower()[:3]


def same_unit(units: list[str]) -> bool:
    """Единица столбца одна и та же. Сравнение по началу слова:
    «минута»/«минут» одно и то же, «минут»/«часов» нет."""
    return len({_unit_stem(u) for u in units}) <= 1


# Длительности: единица -> минуты. Столбец «12 минут / 18 часов / 2 часа»
# это одна величина в разных единицах; на графике она честна только в
# одной (замер процесса согласования: ожидание 18 ч против работы 9 мин).
_DURATION_MINUTES: tuple[tuple[re.Pattern, float], ...] = (
    (re.compile(r"^сек|^с$|^с\.", re.IGNORECASE), 1 / 60),
    (re.compile(r"^мин", re.IGNORECASE), 1.0),
    (re.compile(r"^час|^ч$|^ч\.", re.IGNORECASE), 60.0),
    (re.compile(r"^(дн|ден|сут)", re.IGNORECASE), 1440.0),
    (re.compile(r"^нед", re.IGNORECASE), 10080.0),
)
_HOURS_FROM_MINUTES = 120.0


def _duration_minutes(unit: str) -> float | None:
    unit = (unit or "").strip()
    for pattern, factor in _DURATION_MINUTES:
        if pattern.match(unit):
            return factor
    return None


def normalize_durations(parsed: list[tuple[float, str]]) -> list[tuple[float, str]] | None:
    """Столбец длительностей в разных единицах -> одна единица: часы, если
    самое большое значение не меньше двух часов, иначе минуты. `None`, если
    это не длительности или единица и так одна."""
    factors = [_duration_minutes(u) for _v, u in parsed]
    if not parsed or any(f is None for f in factors) or same_unit([u for _v, u in parsed]):
        return None
    minutes = [v * f for (v, _u), f in zip(parsed, factors)]
    if max(minutes) >= _HOURS_FROM_MINUTES:
        return [(round(m / 60.0, 2), "ч") for m in minutes]
    return [(round(m, 1), "мин") for m in minutes]


def numeric_columns(table: RawTable) -> list[tuple[int, list[tuple[float, str]]]]:
    """Столбцы тела (без первого), где каждая ячейка число одной единицы:
    (номер столбца, [(число, единица)]). Длительности в разных единицах
    приводятся к одной (`normalize_durations`)."""
    body = table.body
    out = []
    for col in range(1, len(table.header)):
        parsed = [parse_number(r[col]) if col < len(r) else None for r in body]
        if not body or any(p is None for p in parsed):
            continue
        if not same_unit([p[1] for p in parsed]):
            parsed = normalize_durations(parsed)
            if parsed is None:
                continue
        out.append((col, parsed))
    return out


def numeric_series_of(table: RawTable) -> NumericSeries | None:
    """Ряд для графика из таблицы или `None`: категории из первого
    столбца, ряды из числовых столбцов одной единицы."""
    body = table.body
    if not body or len(table.header) < 2:
        return None
    columns = numeric_columns(table)[:MAX_SERIES]
    if not columns:
        return None
    series = [SeriesData(name=table.header[col], values=[p[0] for p in parsed][:MAX_POINTS]) for col, parsed in columns]
    units = [p[1] for _col, parsed in columns for p in parsed]
    raw = [r[col] for col, _parsed in columns for r in body]
    return NumericSeries(
        categories=[r[0] for r in body][:MAX_POINTS], series=series, unit=_common_unit(units),
        category_title="" if table.is_list else table.header[0],
        context=" ".join([table.heading, *([] if table.is_list else table.header)]).strip(), raw=raw,
    )


def find_tables(text: str) -> list[RawTable]:
    """Все таблицы и списки «подпись: число» (не короче `MIN_POINTS`
    строк) в порядке появления."""
    lines = (text or "").splitlines()
    found: list[RawTable] = []
    heading = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        h = _HEADING_RE.match(line)
        if h:
            heading = h.group(1)
            i += 1
            continue
        if line.strip().startswith("|") and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            header = _split_row(line)
            j = i + 2
            rows = []
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(_split_row(lines[j]))
                j += 1
            found.append(RawTable(heading=heading, header=header, rows=rows))
            i = j
            continue
        run = []
        j = i
        while j < len(lines) and (m := _LIST_POINT_RE.match(lines[j])):
            run.append(m)
            j += 1
        if len(run) >= MIN_POINTS:
            found.append(RawTable(
                heading=heading, header=[heading or "Показатель", "Значение"],
                rows=[[m.group(1).strip(), f"{m.group(2).strip()} {m.group(3)}".strip()] for m in run],
                is_list=True,
            ))
            i = j
            continue
        i += 1
    return found


def find_numeric_series(text: str) -> list[NumericSeries]:
    """Ряды текста не короче `MIN_POINTS` точек, в порядке появления."""
    out = []
    for table in find_tables(text):
        s = numeric_series_of(table)
        if s is not None and len(s.categories) >= MIN_POINTS:
            out.append(s)
    return out


_WORD_RE = re.compile(r"\w{4,}", re.UNICODE)


def stems(text: str) -> set[str]:
    # Первые пять букв: «регистраций»/«регистрации» и «квартал»/«кварталам»
    # совпадают, а разные слова почти никогда.
    return {w[:5].lower() for w in _WORD_RE.findall(text or "")}
