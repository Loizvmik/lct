"""Схема содержания колоды — что на слайде, а не как оно выглядит.

`DeckSpec`/`SlideSpec`/блоки — граница между работой модели (пишет текст,
Task 11-13) и работой кода (версткой, `deckforge.compose`, эта задача):
модель решает, ЧТО сказать (заголовок, буллеты, цифры KPI), код решает, КАК
это положить на холст (координаты слота, цвет, кегль после ужимания,
буллет-символ шаблона). Поэтому здесь нет ни одного поля, несущего
координату, цвет, шрифт или кегль — ни в блоках, ни в `SlideSpec` целиком;
единственная отсылка к вёрстке — `SlideSpec.kind`, и та ссылается на
`Pattern.kind` (закрытый список видов раскладки, `SLIDE_KINDS` ниже — до
Task 18 их было семь, теперь десять, см. её докстроку), а не на конкретный
`layout_id`/`pattern_id` — выбор КОНКРЕТНОГО паттерна под этот `kind`
(подбор по вместимости) — работа `compose.builder`, не плана.

Модуль намеренно не импортирует `python-pptx` (архитектурная граница
задачи, "Что уже готово" брифа) — план не знает ни одной координаты,
поэтому ему физически нечем работать с объектами python-pptx.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# Замкнутый список — тот же набор значений, что и допустимые `Pattern.kind`
# (геометрические семь из `template/patterns.py::KINDS` плюс три вида Task
# 18 — `quote`/`photo_text`/`kpi_caption`, `config/pattern-kinds.yaml`,
# уточняются мультимодальной моделью поверх геометрии, см. `template.
# vision_kind`), план ссылается на него по смыслу, не по импорту (импорт
# `template.patterns`/чтение YAML сюда добавил бы плану знание о майнинге
# раскладок — задача этого модуля куда уже: описать содержание). Три новых
# вида — не произвольное расширение: `quote`/`kpi_caption` уже совпадают
# ролями, которые `patterns.ROLES` несёт (`"quote"`) или которые снимает
# геометрия (`kpi_value`+`kpi_label`), `photo_text` — содержательно то же
# самое, что уже собирает `compose.blocks` под `Visual(kind="photo")` плюс
# текстовый блок, просто раскладка шаблона под это сочетание раньше не
# отличалась от обычного текстового слайда (`bullets`) и терялась в подборе.
SLIDE_KINDS = (
    "cards", "two_col", "kpi", "section", "image", "table", "bullets",
    "quote", "photo_text", "kpi_caption",
)


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
class ChartSeriesData:
    """Один ряд данных графика — план несёт числа и подпись ряда, цвет
    (и то, рисовать ли по рядам или по точкам) решает `compose.charts`, не
    план (тот же принцип разделения, что у `TableVisual`/`Kpi.value`)."""
    name: str
    values: list[float]


@dataclass(frozen=True)
class ChartVisual:
    """Данные графика — план ссылается на набор типов, которые умеет рисовать
    `compose.charts.CHART_KINDS` ("bar"/"bar_stacked"/"bar_h"/"line"/"area"/
    "pie"/"doughnut"/"scatter"), по смыслу строкой, не импортируя `compose`
    (план не знает о вёрстке, тот же принцип, что и `SLIDE_KINDS` выше
    ссылается на `Pattern.kind` по смыслу, не по импорту)."""
    kind: str
    categories: list[str]
    series: list[ChartSeriesData]
    unit: str | None = None
    highlight_index: int | None = None
    axis_titles: tuple[str, str] | None = None


@dataclass(frozen=True)
class Visual:
    """Что должно занять нетекстовый слот раскладки (роль `image`/`icon`/
    `chart`/`table` у `Pattern.slots`) — тоже описание содержания, не
    вёрстки: КАКОЙ ассет каталога шаблона подходит по смыслу (`asset_role`,
    например "photo"/"icon"/"logo") или какие данные несёт таблица
    (`table`)/график (`chart`), а не координаты и не то, как картинка
    обрезана под рамку.

    `photo_name` (Task 20, встраивание пользовательских фотографий) — имя
    файла фотографии контент-пакета (`plan.photos.ContentPhoto.name`),
    которую `plan.photos.assign_photos` поставила на этот слайд, или
    `None` — вёрстка тогда берёт фото из каталога ассетов ШАБЛОНА, как и
    до этой задачи (`compose.builder._place_picture_visual`). Только имя
    файла, не путь и не байты — тот же принцип, что и у `asset_role` в
    докстроке выше: план знает, ЧТО показать по смыслу, не пиксели и не
    координаты; сами байты фотографии сборка достаёт по этому имени из
    словаря, который ей передаёт вызывающий код (`cli.py`), а не план.
    Слайд-райтер (`agents/slide-writer/AGENT.md`) это поле никогда не
    заполняет — его нет среди `_SLIDE_ALLOWED`/ключей `visual_from_dict`
    ниже (тот же приём, что и `SlideSpec.pattern_id`, который тоже
    проставляется ПОСЛЕ содержания отдельным шагом, не моделью, писавшей
    текст)."""
    kind: str  # "photo" | "icon" | "chart" | "table"
    caption: str | None = None
    photo_name: str | None = None
    table: TableVisual | None = None
    chart: ChartVisual | None = None


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
    generation_origin: str = "model"
    generation_error: str | None = None
    # Task 13: raскладка КОНКРЕТНОГО паттерна (`Pattern.pattern_id`), а не
    # только желаемого `kind` — проставляется `plan.variants.apply_variant`
    # (детерминированно, по вместимости) и/или `plan.writer.pick_patterns`
    # (моделью, поверх уже проставленного кода — см. докстроки обоих). `None`
    # — раскладку выбирает `compose.builder._pick_pattern` по `kind`, тот же
    # путь, каким собирается `SAMPLE_SPEC`/`CARDS_SPEC` в
    # `tests/compose/test_builder.py` (Task 9-10, до этой задачи) — обратная
    # совместимость сохранена буквально: старый код, не знающий про
    # `pattern_id`, продолжает работать без единой правки.
    pattern_id: str | None = None


@dataclass
class DeckSpec:
    title: str
    language: str
    slides: list[SlideSpec] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Валидатор — ловит то, что иначе всплывёт только при сборке (Task 13 брифа,
# "Требования к работе"): незнакомое поле (опечатка — ошибка, а не тишина),
# пустой обязательный текст, расхождение длины ряда данных с числом
# категорий, расхождение длины строки таблицы с шапкой, отсутствие `source`
# при наличии цифр. Намеренно НЕ pydantic (весь модуль — датаклассы без
# внешней зависимости, тот же принцип "план не знает ни одной координаты" —
# заводить pydantic только ради валидации значило бы тянуть отдельный слой
# сериализации туда, где формат уже прост и закрыт).
# ---------------------------------------------------------------------------


class SpecValidationError(ValueError):
    """Список найденных проблем `DeckSpec`/сырого JSON — не первая же
    проблема (брифом: "ловит", не "ловит одну за раз") — вызывающему коду
    (writer.py) нужен полный список сразу, а не цикл повторных попыток по
    одной ошибке."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


_DIGIT_RE = re.compile(r"\d")


def _has_digits(*texts: str | None) -> bool:
    return any(t and _DIGIT_RE.search(t) for t in texts)


def slide_spec_problems(slide: SlideSpec) -> list[str]:
    """Проблемы ОДНОГО слайда — вынесено из `validate_deck_spec` отдельной
    функцией, чтобы `writer.write_slides` могло проверить только что
    написанный моделью слайд сразу (до сборки всей колоды) и, если он
    невалиден, повторить попытку или деградировать до запасного варианта,
    не проверяя (и не имея под рукой) остальные слайды колоды."""
    problems: list[str] = []
    where = f"слайд {slide.index}"
    if slide.kind not in SLIDE_KINDS:
        problems.append(f"{where}: неизвестный kind={slide.kind!r} (ожидается один из {SLIDE_KINDS})")
    if not slide.headline or not slide.headline.strip():
        problems.append(f"{where}: пустой обязательный headline")

    has_digits = _has_digits(slide.headline, slide.subhead)

    for block in slide.blocks:
        if isinstance(block, TextBlock):
            if not block.text.strip():
                problems.append(f"{where}: пустой TextBlock.text")
            has_digits = has_digits or _has_digits(block.text)
        elif isinstance(block, BulletBlock):
            if not block.items:
                problems.append(f"{where}: BulletBlock без пунктов")
            for i, item in enumerate(block.items):
                if not item.strip():
                    problems.append(f"{where}: пустой пункт {i} в BulletBlock")
                has_digits = has_digits or _has_digits(item)
        elif isinstance(block, CardBlock):
            if not block.items:
                problems.append(f"{where}: CardBlock без карточек")
            for i, card in enumerate(block.items):
                if not card.body.strip():
                    problems.append(f"{where}: пустое тело карточки {i}")
                has_digits = has_digits or _has_digits(card.body, card.title)
        elif isinstance(block, KpiBlock):
            if not block.items:
                problems.append(f"{where}: KpiBlock без метрик")
            for i, kpi in enumerate(block.items):
                if not kpi.value.strip():
                    problems.append(f"{where}: пустое значение KPI {i}")
                if not kpi.label.strip():
                    problems.append(f"{where}: пустая подпись KPI {i}")
            if block.items:
                has_digits = True  # KPI по природе несёт число — вход
        elif isinstance(block, QuoteBlock):
            if not block.text.strip():
                problems.append(f"{where}: пустая цитата")

    if slide.visual is not None:
        table = slide.visual.table
        if table is not None:
            if not table.rows:
                problems.append(f"{where}: TableVisual без строк")
            else:
                header_len = len(table.rows[0])
                for i, row in enumerate(table.rows[1:], start=1):
                    if len(row) != header_len:
                        problems.append(
                            f"{where}: строка {i} таблицы несёт {len(row)} ячеек, "
                            f"а шапка — {header_len}"
                        )
                has_digits = has_digits or any(
                    _has_digits(cell) for row in table.rows for cell in row
                )
        chart = slide.visual.chart
        if chart is not None:
            n_cat = len(chart.categories)
            if not chart.series:
                problems.append(f"{where}: ChartVisual без рядов данных")
            for series in chart.series:
                if len(series.values) != n_cat:
                    problems.append(
                        f"{where}: ряд {series.name!r} несёт {len(series.values)} значений, "
                        f"а категорий {n_cat}"
                    )
            has_digits = True

    if has_digits and not (slide.source_note and slide.source_note.strip()):
        problems.append(f"{where}: на слайде есть цифры, а source_note не указан")

    return problems


def validate_deck_spec(spec: DeckSpec) -> list[str]:
    """Список проблем `spec` (пустой список — валиден). Не бросает
    исключение сама — `ensure_valid_deck_spec` ниже оборачивает список в
    `SpecValidationError`, когда вызывающему коду нужно исключение, а не
    список (тесты и код-ревью содержимого могут хотеть список как есть)."""
    problems: list[str] = []
    if not spec.title or not spec.title.strip():
        problems.append("DeckSpec.title пуст")
    if not spec.slides:
        problems.append("DeckSpec.slides пуст — колоде нечего собирать")

    for slide in spec.slides:
        problems.extend(slide_spec_problems(slide))

    return problems


def ensure_valid_deck_spec(spec: DeckSpec) -> DeckSpec:
    problems = validate_deck_spec(spec)
    if problems:
        raise SpecValidationError(problems)
    return spec


# ---------------------------------------------------------------------------
# Строгий парсер JSON модели — незнакомое поле (опечатка) обязано быть
# ошибкой, а не тихо проигнорированным ключом (Task 13 брифа, "Требования к
# работе"). Явные перечни разрешённых/обязательных ключей на каждом уровне
# (не generic-рефлексия по датаклассам) — тот же стиль, что и остальной
# проект (`profile.py`, `naming.py`): явное сравнение множеств ключей проще
# проверить глазами, чем магию через `dataclasses.fields`.
# ---------------------------------------------------------------------------


def _check_keys(data: dict, allowed: set[str], required: set[str], where: str) -> None:
    if not isinstance(data, dict):
        raise SpecValidationError([f"{where}: ожидался объект JSON, получено {type(data).__name__}"])
    unknown = set(data) - allowed
    if unknown:
        raise SpecValidationError([f"{where}: неизвестные поля {sorted(unknown)}"])
    missing = required - set(data)
    if missing:
        raise SpecValidationError([f"{where}: отсутствуют обязательные поля {sorted(missing)}"])


def block_from_dict(data: dict, where: str) -> Block:
    if not isinstance(data, dict) or "type" not in data:
        raise SpecValidationError([f"{where}: блок должен быть объектом с полем 'type'"])
    kind = data["type"]
    if kind == "text":
        _check_keys(data, {"type", "text"}, {"type", "text"}, where)
        return TextBlock(text=data["text"])
    if kind == "bullets":
        _check_keys(data, {"type", "items"}, {"type", "items"}, where)
        return BulletBlock(items=[str(i) for i in data["items"]])
    if kind == "cards":
        _check_keys(data, {"type", "items"}, {"type", "items"}, where)
        cards = []
        for i, c in enumerate(data["items"]):
            _check_keys(c, {"title", "body"}, {"body"}, f"{where}.items[{i}]")
            cards.append(Card(body=c["body"], title=c.get("title", "")))
        return CardBlock(items=cards)
    if kind == "kpi":
        _check_keys(data, {"type", "items"}, {"type", "items"}, where)
        kpis = []
        for i, k in enumerate(data["items"]):
            _check_keys(k, {"value", "label"}, {"value", "label"}, f"{where}.items[{i}]")
            kpis.append(Kpi(value=k["value"], label=k["label"]))
        return KpiBlock(items=kpis)
    if kind == "quote":
        _check_keys(data, {"type", "text", "author"}, {"type", "text"}, where)
        return QuoteBlock(text=data["text"], author=data.get("author"))
    raise SpecValidationError([f"{where}: неизвестный тип блока {kind!r}"])


def visual_from_dict(data: dict | None, where: str) -> Visual | None:
    if data is None:
        return None
    _check_keys(data, {"kind", "caption", "table", "chart"}, {"kind"}, where)

    table = None
    if data.get("table") is not None:
        t = data["table"]
        _check_keys(t, {"rows"}, {"rows"}, f"{where}.table")
        table = TableVisual(rows=[[str(cell) for cell in row] for row in t["rows"]])

    chart = None
    if data.get("chart") is not None:
        c = data["chart"]
        _check_keys(
            c, {"kind", "categories", "series", "unit", "highlight_index", "axis_titles"},
            {"kind", "categories", "series"}, f"{where}.chart",
        )
        series = []
        for i, s in enumerate(c["series"]):
            _check_keys(s, {"name", "values"}, {"name", "values"}, f"{where}.chart.series[{i}]")
            series.append(ChartSeriesData(name=s["name"], values=[float(v) for v in s["values"]]))
        axis_titles_raw = c.get("axis_titles")
        axis_titles = (str(axis_titles_raw[0]), str(axis_titles_raw[1])) if axis_titles_raw else None
        chart = ChartVisual(
            kind=c["kind"], categories=[str(cat) for cat in c["categories"]], series=series,
            unit=c.get("unit"), highlight_index=c.get("highlight_index"), axis_titles=axis_titles,
        )

    return Visual(kind=data["kind"], caption=data.get("caption"), table=table, chart=chart)


# `layout_id` (Task 23) — номер раскладки, которую агент выбрал сам,
# посмотрев каталог шаблона и собрав слайд начерно (`compose.slide_tools`).
# Это имя, а не координата: граница «план не знает ни одной координаты»
# цела — что именно стоит за этим номером (рамки, кегли, цвета), знает
# только `compose/`. Поле необязательное: слайд без него собирается как
# раньше — раскладку подбирает код по вместимости.
_SLIDE_ALLOWED = {
    "kind", "headline", "subhead", "blocks", "visual", "source_note", "speaker_notes", "layout_id",
}
_SLIDE_REQUIRED = {"kind", "headline"}


def slide_spec_from_dict(data: dict, index: int) -> SlideSpec:
    where = f"слайд {index}"
    _check_keys(data, _SLIDE_ALLOWED, _SLIDE_REQUIRED, where)
    blocks = [block_from_dict(b, f"{where}.blocks[{i}]") for i, b in enumerate(data.get("blocks", []) or [])]
    return SlideSpec(
        index=index, kind=data["kind"], headline=data["headline"], subhead=data.get("subhead"),
        blocks=blocks, visual=visual_from_dict(data.get("visual"), f"{where}.visual"),
        source_note=data.get("source_note"), speaker_notes=data.get("speaker_notes"),
        pattern_id=data.get("layout_id"),
    )


_DECK_ALLOWED = {"title", "language", "slides", "meta"}
_DECK_REQUIRED = {"title", "language", "slides"}


def deck_spec_from_dict(data: dict) -> DeckSpec:
    where = "DeckSpec"
    _check_keys(data, _DECK_ALLOWED, _DECK_REQUIRED, where)
    slides = [slide_spec_from_dict(s, i) for i, s in enumerate(data["slides"])]
    return DeckSpec(title=data["title"], language=data["language"], slides=slides, meta=dict(data.get("meta", {}) or {}))


# ---------------------------------------------------------------------------
# deck_spec_to_dict / deck_spec_from_debug_dict — round-trip сериализация
# СОБСТВЕННОГО (не модельного) `DeckSpec`, отдельно от `*_from_dict` выше.
#
# `*_from_dict` (выше) — граница с МОДЕЛЬЮ: строгая (типоопечатка в поле —
# ошибка, не тишина, `_check_keys`), и намеренно не несёт `index`/`findings`/
# `pattern_id` (`_SLIDE_ALLOWED`) — эти поля модель не пишет, их проставляет
# код (docstring `SlideSpec`). Пара ниже — граница с ДИСКОМ: `cli._cmd_
# generate` уже дампит написанный `DeckSpec` в JSON "для отладки"
# (`debug_path`); этой задаче (вынос аудита по картинке в отдельную
# команду) нужно этот же дамп полноценно ЧИТАТЬ ОБРАТНО — `deckforge
# audit-visual` запускается на уже готовом `.pptx` без повторного похода к
# модели (`run_visual` не зависит от сборки, см. docstring команды в
# `cli.py`), а для промпта аудита (заголовок колоды/языка/соседних слайдов,
# `audit.visual._build_slide_prompt`) нужен настоящий `DeckSpec`, не только
# .pptx-геометрия. Свой формат (JSON-объект с `"type"` у каждого блока,
# те же имена полей, что и `*_from_dict`) — не совпадает буквально с сырым
# дампом `dataclasses.fields` (там раньше был `cli._spec_to_json`: без тега
# `"type"` у блока `{"text": ...}` неотличим от `TextBlock` и `QuoteBlock`
# без `author`) — не доверять типовому выводу небезопасно, дамп/загрузка
# должны быть ПАРОЙ одного формата, а не угадыванием по форме словаря.
# Не валидирует чужой ввод (в отличие от `*_from_dict`) — источник данных
# всегда файл, который написал этот же код мгновением раньше, разбор
# по смыслу ближе к десериализации, а не к проверке чужого JSON.
# ---------------------------------------------------------------------------


def _block_to_dict(block: Block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, BulletBlock):
        return {"type": "bullets", "items": list(block.items)}
    if isinstance(block, CardBlock):
        return {"type": "cards", "items": [{"title": c.title, "body": c.body} for c in block.items]}
    if isinstance(block, KpiBlock):
        return {"type": "kpi", "items": [{"value": k.value, "label": k.label} for k in block.items]}
    if isinstance(block, QuoteBlock):
        return {"type": "quote", "text": block.text, "author": block.author}
    raise TypeError(f"deck_spec_to_dict: неизвестный тип блока {type(block).__name__}")


def _block_from_debug_dict(data: dict) -> Block:
    kind = data["type"]
    if kind == "text":
        return TextBlock(text=data["text"])
    if kind == "bullets":
        return BulletBlock(items=list(data["items"]))
    if kind == "cards":
        return CardBlock(items=[Card(body=c["body"], title=c.get("title", "")) for c in data["items"]])
    if kind == "kpi":
        return KpiBlock(items=[Kpi(value=k["value"], label=k["label"]) for k in data["items"]])
    if kind == "quote":
        return QuoteBlock(text=data["text"], author=data.get("author"))
    raise ValueError(f"deck_spec_from_debug_dict: неизвестный тип блока в дампе {kind!r}")


def _visual_to_dict(visual: Visual | None) -> dict | None:
    if visual is None:
        return None
    table = {"rows": [list(row) for row in visual.table.rows]} if visual.table is not None else None
    chart = None
    if visual.chart is not None:
        c = visual.chart
        chart = {
            "kind": c.kind, "categories": list(c.categories),
            "series": [{"name": s.name, "values": list(s.values)} for s in c.series],
            "unit": c.unit, "highlight_index": c.highlight_index,
            "axis_titles": list(c.axis_titles) if c.axis_titles else None,
        }
    return {
        "kind": visual.kind, "caption": visual.caption, "photo_name": visual.photo_name,
        "table": table, "chart": chart,
    }


def _visual_from_debug_dict(data: dict | None) -> Visual | None:
    if data is None:
        return None
    table = TableVisual(rows=[list(row) for row in data["table"]["rows"]]) if data.get("table") else None
    chart = None
    if data.get("chart"):
        c = data["chart"]
        chart = ChartVisual(
            kind=c["kind"], categories=list(c["categories"]),
            series=[ChartSeriesData(name=s["name"], values=list(s["values"])) for s in c["series"]],
            unit=c.get("unit"), highlight_index=c.get("highlight_index"),
            axis_titles=tuple(c["axis_titles"]) if c.get("axis_titles") else None,
        )
    return Visual(
        kind=data["kind"], caption=data.get("caption"), photo_name=data.get("photo_name"),
        table=table, chart=chart,
    )


def slide_spec_to_dict(slide: SlideSpec) -> dict:
    return {
        "index": slide.index, "kind": slide.kind, "headline": slide.headline, "subhead": slide.subhead,
        "blocks": [_block_to_dict(b) for b in slide.blocks],
        "visual": _visual_to_dict(slide.visual),
        "source_note": slide.source_note, "speaker_notes": slide.speaker_notes,
        "findings": list(slide.findings), "pattern_id": slide.pattern_id,
        "generation_origin": slide.generation_origin, "generation_error": slide.generation_error,
    }


def slide_spec_from_debug_dict(data: dict) -> SlideSpec:
    return SlideSpec(
        index=data["index"], kind=data["kind"], headline=data["headline"], subhead=data.get("subhead"),
        blocks=[_block_from_debug_dict(b) for b in (data.get("blocks") or [])],
        visual=_visual_from_debug_dict(data.get("visual")),
        source_note=data.get("source_note"), speaker_notes=data.get("speaker_notes"),
        findings=list(data.get("findings") or []), pattern_id=data.get("pattern_id"),
        generation_origin=data.get("generation_origin", "model"),
        generation_error=data.get("generation_error"),
    )


def deck_spec_to_dict(spec: DeckSpec) -> dict:
    return {
        "title": spec.title, "language": spec.language,
        "slides": [slide_spec_to_dict(s) for s in spec.slides],
        "meta": dict(spec.meta),
    }


def deck_spec_from_debug_dict(data: dict) -> DeckSpec:
    return DeckSpec(
        title=data["title"], language=data["language"],
        slides=[slide_spec_from_debug_dict(s) for s in data["slides"]],
        meta=dict(data.get("meta") or {}),
    )
