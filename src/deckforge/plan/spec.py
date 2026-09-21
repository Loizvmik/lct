"""Схема содержания колоды — что на слайде, а не как оно выглядит.

`DeckSpec`/`SlideSpec`/блоки — граница между работой модели (пишет текст,
Task 11-13) и работой кода (версткой, `deckforge.compose`, эта задача):
модель решает, ЧТО сказать (заголовок, буллеты, цифры KPI), код решает, КАК
это положить на холст (координаты слота, цвет, кегль после ужимания,
буллет-символ шаблона). Поэтому здесь нет ни одного поля, несущего
координату, цвет, шрифт или кегль — ни в блоках, ни в `SlideSpec` целиком;
единственная отсылка к вёрстке — `SlideSpec.kind`, и та ссылается на
`Pattern.kind` (один из семи закрытых типов раскладки: "cards" | "two_col"
| "kpi" | "section" | "image" | "table" | "bullets"), а не на конкретный
`layout_id`/`pattern_id` — выбор КОНКРЕТНОГО паттерна под этот `kind`
(подбор по вместимости) — работа `compose.builder`, не плана.

Модуль намеренно не импортирует `python-pptx` (архитектурная граница
задачи, "Что уже готово" брифа) — план не знает ни одной координаты,
поэтому ему физически нечем работать с объектами python-pptx.
"""
from __future__ import annotations
from dataclasses import dataclass, field

# Замкнутый список — тот же семь значений, что и `Pattern.kind` в
# `template/patterns.py` (KINDS), план ссылается на него по смыслу, не по
# импорту (импорт `template.patterns` сюда добавил бы плану знание о
# майнинге раскладок — задача этого модуля куда уже: описать содержание).
SLIDE_KINDS = ("cards", "two_col", "kpi", "section", "image", "table", "bullets")


@dataclass(frozen=True)
class TextBlock:
    """Один связный абзац — вступление, вывод, короткий комментарий."""
    text: str


@dataclass(frozen=True)
class BulletBlock:
    """Список пунктов — буллет-символ и его цвет берёт раскладка шаблона
    (`compose.blocks`), не план: план знает только текст каждого пункта."""
    items: list[str]


@dataclass(frozen=True)
class Card:
    """Одна карточка внутри `CardBlock` — заголовок карточки может быть
    пустым (не у каждой карточки шаблона есть заголовок отдельно от тела,
    см. `Pattern.slots` с ролью только `card_body`), тело — нет."""
    body: str
    title: str = ""


@dataclass(frozen=True)
class CardBlock:
    """Ряд/сетка однородных карточек — число элементов произвольно, сборщик
    разворачивает `RepeatSpec` раскладки под фактическое число (бриф:
    "повтор разворачивай под фактическое число элементов, пересчитывая
    шаг"), не под то, сколько карточек было на слайде-примере."""
    items: list[Card]


@dataclass(frozen=True)
class Kpi:
    """Одна метрика — `value` уже отформатировано как строка ("−80%",
    "31 ч 30 мин"): план работает с готовым для показа текстом, не с
    сырым числом и единицей измерения по отдельности — форматирование
    чисел решает автор контента (модель/бриф), не вёрстка."""
    value: str
    label: str


@dataclass(frozen=True)
class KpiBlock:
    items: list[Kpi]


@dataclass(frozen=True)
class QuoteBlock:
    text: str
    author: str | None = None


Block = TextBlock | BulletBlock | CardBlock | KpiBlock | QuoteBlock


@dataclass(frozen=True)
class TableVisual:
    """Содержимое таблицы — строки ячеек, первая строка — шапка. Числа и
    подписи уже готовым текстом (тот же принцип, что `Kpi.value`)."""
    rows: list[list[str]]


@dataclass(frozen=True)
class Visual:
    """Что должно занять нетекстовый слот раскладки (роль `image`/`icon`/
    `chart`/`table` у `Pattern.slots`) — тоже описание содержания, не
    вёрстки: КАКОЙ ассет каталога шаблона подходит по смыслу (`asset_role`,
    например "photo"/"icon"/"logo") или какие данные несёт таблица
    (`table`), а не координаты и не то, как картинка обрезана под рамку.
    """
    kind: str  # "photo" | "icon" | "chart" | "table"
    caption: str | None = None
    table: TableVisual | None = None


@dataclass
class SlideSpec:
    """Один слайд будущей колоды. `kind` — желаемый тип раскладки
    (`SLIDE_KINDS`), КАКОЙ конкретно `Pattern` под него подобрать — решает
    `compose.builder.build_deck` по вместимости (Task 13 связывает это с
    подбором самой модели; здесь `kind` может быть проставлен и вручную,
    ровно так вызывающий код Step 4 брифа собирает `SAMPLE_SPEC` в
    `tests/compose/test_builder.py`).

    `findings` — единственное мутируемое поле не про содержание, а про
    честность сборки (бриф, "Требования к работе": "молча обрезать
    нельзя, это увидит аудит") — сюда `compose.builder` дописывает
    находку, когда содержание пришлось усечь ниже подписи, чтобы влезло в
    слот; план сам в это поле не пишет, оно всегда пустое на выходе
    планировщика/модели и заполняется только сборкой."""
    index: int
    kind: str
    headline: str
    subhead: str | None = None
    blocks: list[Block] = field(default_factory=list)
    visual: Visual | None = None
    source_note: str | None = None
    speaker_notes: str | None = None
    findings: list[str] = field(default_factory=list)


@dataclass
class DeckSpec:
    title: str
    language: str
    slides: list[SlideSpec] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
