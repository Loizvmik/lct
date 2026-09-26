"""Что раскладка шаблона умеет держать, в единицах содержания, а не в
координатах: сколько карточек, пунктов или показателей, сколько слов в
каждом месте, есть ли место под таблицу, график, фото.

Вид раскладки (`Pattern.kind`) снят геометрией и моделью и не всегда
говорит, куда реально ляжет текст: у VK Education `slide22` помечен как
`cards`, а несёт пары «число + подпись» без повтора. Сборка кладёт
содержание по РОЛЯМ слотов (`compose.blocks`), поэтому и форма здесь
выводится из ролей тем же порядком: повтор карточек, показатели, цитата,
текстовые места. Одна и та же функция нужна планировщику (хватает ли
места) и контракту слайда (сколько писать), чтобы они не разошлись.

Граница слоёв: читаются только роли, пределы слов и знаков, повтор и число
декора. Ни одной координаты."""
from __future__ import annotations
from dataclasses import dataclass

from deckforge.template.patterns import (
    CHART_TIER_TEXT, CHARS_PER_WORD, chart_target_slot, keeps_sample_text, slot_char_capacity,
)

TEXT_ROLES = ("body", "bullet", "card_body")
_UNIT_BODY_ROLES = ("card_body", "bullet", "body")
# Пунктов в одном месте-списке не больше этого: дальше список читается
# стеной текста, как бы ни было велико место.
MAX_LIST_ITEMS = 6
# Заголовок-вывод короче этого в знаки не сформулировать. Рамка заголовка
# у части примеров VK Education снята под два-три слова («Слайды со
# скриншотами», 13 знаков; подпись на 9 знаков), и контракт с таким
# пределом писатель нарушал всегда (живой прогон 27 сентября 2026).
MIN_HEADLINE_CHARS = 24
# Самый короткий пункт списка, под который делится место-список. Меньше
# трёх слов пункт уже не утверждение.
_MIN_LIST_ITEM_WORDS = 3


@dataclass(frozen=True)
class Limit:
    """Предел одного места в словах и знаках. `max_chars` 0: знаков модель
    разбора не назвала и геометрия не сняла, мерить нечем."""
    max_words: int
    max_chars: int

    @property
    def target_words(self) -> int:
        # Та же доля 0,8, что у `compose.fit_check.target_of`: цель рядом с
        # пределом, иначе модель пишет «как можно короче».
        return max(1, round(self.max_words * 0.8))


@dataclass(frozen=True)
class FormPart:
    """Одна часть содержания раскладки: блок ответа писателя.

    `block`: text / bullets / cards / kpi / quote. `units`: сколько единиц
    часть держит (карточек, пунктов, показателей). `unit`: предел одной
    единицы (у карточки это тело). `title`: предел заголовка карточки или
    подписи показателя. `title_slot`: у единицы есть своё место под
    заголовок; если нет, заголовок сборка склеит с телом жирным абзацем."""
    block: str
    units: int
    unit: Limit
    role: str
    title: Limit | None = None
    title_slot: bool = False
    purpose: str | None = None
    content_hint: str | None = None
    required: bool = True


@dataclass(frozen=True)
class PatternForm:
    pattern_id: str
    kind: str
    parts: tuple[FormPart, ...]
    headline: Limit | None
    subhead: Limit | None
    has_table: bool
    has_chart: bool
    has_image: bool
    decor: int
    repeated: bool
    # Задача V1: насколько раскладка годится под график
    # (`patterns.CHART_TIER_*`: родной график, картинка-график примера,
    # крупное текстовое место), `None`: графику места нет.
    chart_tier: int | None = None
    # Класс слайда-примера (`template.prototypes.SLIDE_CLASSES`).
    slide_class: str = "content_pattern"

    @property
    def main(self) -> FormPart | None:
        """Главная часть: первая обязательная. По ней считается, сколько
        единиц содержания раскладка держит."""
        return next((p for p in self.parts if p.required), None)

    @property
    def units(self) -> int:
        main = self.main
        return main.units if main is not None else 0


def _limit(slot) -> Limit:
    chars = slot_char_capacity(slot) if slot.max_chars > 0 else 0
    words = slot.max_words or (max(1, chars // CHARS_PER_WORD) if chars else 0)
    return Limit(max_words=int(words), max_chars=int(chars))


def _biggest(slots):
    return max(slots, key=lambda s: (slot_char_capacity(s), s.max_words or 0), default=None)


def _open(slots, role: str):
    return [s for s in slots if s.role == role and not keeps_sample_text(s)]


def pattern_form(pattern) -> PatternForm:
    """Форма раскладки. Порядок ветвей повторяет `compose.blocks`: карточки
    разворачивают повтор, показатели ищут пары `kpi_value`/`kpi_label`,
    цитата свой слот, текст и список самое ёмкое текстовое место."""
    slots = list(pattern.slots)
    repeat = pattern.repeat if (pattern.repeat is not None and pattern.repeat.count >= 2) else None
    repeat_roles = set(repeat.slot_roles) if repeat is not None else set()
    parts: list[FormPart] = []
    used_roles: set[str] = set()

    if repeat is not None and repeat_roles & set(_UNIT_BODY_ROLES):
        body_role = next(r for r in _UNIT_BODY_ROLES if r in repeat_roles)
        body = _biggest(_open(slots, body_role))
        if body is not None:
            titles = _open(slots, "card_title") if "card_title" in repeat_roles else []
            title_slot = _biggest(titles)
            parts.append(FormPart(
                block="cards", units=repeat.count, unit=_limit(body), role=body_role,
                title=_limit(title_slot) if title_slot is not None else Limit(max_words=3, max_chars=3 * CHARS_PER_WORD),
                title_slot=title_slot is not None, purpose=body.purpose, content_hint=body.content_hint,
            ))
            used_roles |= repeat_roles
    elif repeat is not None and "kpi_value" in repeat_roles:
        values = _open(slots, "kpi_value")
        labels = _open(slots, "kpi_label")
        if values:
            label = _biggest(labels)
            parts.append(FormPart(
                block="kpi", units=repeat.count, unit=_limit(_biggest(values)), role="kpi_value",
                title=_limit(label) if label is not None else None, title_slot=label is not None,
            ))
            used_roles |= repeat_roles

    if not parts:
        values = _open(slots, "kpi_value")
        labels = _open(slots, "kpi_label")
        if values and labels:
            parts.append(FormPart(
                block="kpi", units=min(len(values), len(labels)), unit=_limit(_biggest(values)),
                role="kpi_value", title=_limit(_biggest(labels)), title_slot=True,
            ))
            used_roles |= {"kpi_value", "kpi_label"}

    quote = _biggest(_open(slots, "quote"))
    if not parts and quote is not None:
        parts.append(FormPart(block="quote", units=1, unit=_limit(quote), role="quote",
                              purpose=quote.purpose, content_hint=quote.content_hint))
        used_roles.add("quote")

    # Текстовые места вне повтора: у раскладки без повтора это главное
    # содержание (абзац или список), у раскладки с повтором вводный абзац.
    free = [
        s for s in slots
        if s.role in TEXT_ROLES and s.role not in used_roles and not keeps_sample_text(s) and s.max_chars > 0
    ]
    free.sort(key=lambda s: -slot_char_capacity(s))
    if free and not parts:
        main = free[0]
        limit = _limit(main)
        list_units = max(1, min(MAX_LIST_ITEMS, limit.max_words // _MIN_LIST_ITEM_WORDS or 1))
        parts.append(FormPart(
            block="bullets" if list_units >= 2 else "text", units=list_units, unit=limit, role=main.role,
            purpose=main.purpose, content_hint=main.content_hint,
        ))
        free = free[1:]
    for extra in free[:2]:
        # Второе и третье текстовое место: колонка у двухколоночной
        # раскладки или вводный абзац над карточками. Необязательны: у
        # раскладки их может быть больше, чем есть что сказать.
        parts.append(FormPart(
            block="text", units=1, unit=_limit(extra), role=extra.role,
            purpose=extra.purpose, content_hint=extra.content_hint, required=False,
        ))

    headline = _biggest([s for s in slots if s.role == "headline"])
    subhead = _biggest([s for s in slots if s.role in ("subhead", "caption") and not keeps_sample_text(s)])
    roles = {s.role for s in slots}
    return PatternForm(
        pattern_id=pattern.pattern_id, kind=pattern.kind, parts=tuple(parts),
        headline=_limit(headline) if headline is not None else None,
        subhead=_limit(subhead) if subhead is not None else None,
        has_table="table" in roles or pattern.kind == "table",
        has_chart="chart" in roles,
        has_image="image" in roles,
        decor=len(pattern.decor), repeated=repeat is not None,
        chart_tier=_chart_tier(pattern, parts),
        slide_class=getattr(pattern, "slide_class", "content_pattern"),
    )


def _chart_tier(pattern, parts: list[FormPart]) -> int | None:
    """Ступень пригодности под график. Текстовое место годится только у
    раскладки, чьё главное содержание абзац или список: график в одной
    карточке из трёх ломает ряд, а не заменяет содержание."""
    _slot, tier = chart_target_slot(pattern.slots)
    if tier == CHART_TIER_TEXT:
        main = next((p for p in parts if p.required), None)
        if main is None or main.block not in ("bullets", "text"):
            return None
    return tier


def list_item_limit(part: FormPart, count: int) -> Limit:
    """Предел одного пункта, когда список из `count` пунктов ложится в ОДНО
    место: место делится поровну. У карточек и показателей своё место на
    единицу, предел не делится."""
    if part.block != "bullets" or count <= 1:
        return part.unit
    words = max(_MIN_LIST_ITEM_WORDS, part.unit.max_words // count)
    chars = max(0, part.unit.max_chars // count - 1) if part.unit.max_chars else 0
    return Limit(max_words=words, max_chars=chars)


def forms_of(profile) -> dict[str, PatternForm]:
    return {p.pattern_id: pattern_form(p) for p in profile.patterns}


# ---------------------------------------------------------------------------
# Возможности раскладки и требования слайда (задача V2)
# ---------------------------------------------------------------------------

# Пустота на месте удалённого фото примера, доля холста, выше которой
# раскладка без своего фото не годится: половина слайда белым листом.
MAX_PHOTO_VOID = 0.30

_TEXT_PLACE_ROLES = ("body", "bullet", "card_body", "quote")


@dataclass(frozen=True)
class PatternCapabilities:
    """Что раскладка умеет держать, явными полями, а не видом `kind`.

    Вид снят геометрией и моделью и остаётся предпочтением стиля, но
    решать допуск по нему нельзя: у VK Education вид `image` у диаграммы
    Ганта (пять карточек) и у «Паттерн + фото» (один абзац и фото
    лейаута), и карточки, написанные под первую, уезжали во вторую.
    Допуск раскладки к слайду: требования слайда входят в возможности
    (`unmet_requirements`)."""
    headline: bool
    text_blocks_min: int
    text_blocks_max: int
    card_count_min: int
    card_count_max: int
    card_has_title: bool
    card_has_body: bool
    card_has_icon: bool
    card_has_image: bool
    supports_table: bool
    supports_chart: bool
    supports_photo: bool
    supports_quote: bool
    supports_kpi: bool
    kpi_count_min: int
    kpi_count_max: int
    repeat_topology: str
    sequence_semantics: bool
    max_total_words: int
    # Фото-образцы примера (`Pattern.photo_*`): сколько их, доля холста
    # всех и доля тех, что стоят в месте `image` (туда ляжет наше фото).
    photo_frames: int = 0
    photo_area: float = 0.0
    photo_slot_area: float = 0.0

    def photo_void(self, has_photo: bool) -> float:
        """Доля холста, которая опустеет, когда клон удалит фото примера."""
        area = self.photo_area - (self.photo_slot_area if has_photo else 0.0)
        return max(0.0, area)


def capabilities_of(pattern, form: PatternForm | None = None) -> PatternCapabilities:
    form = form or pattern_form(pattern)
    repeat = pattern.repeat if (pattern.repeat is not None and pattern.repeat.count >= 2) else None
    repeat_roles = set(repeat.slot_roles) if repeat is not None else set()
    cards = next((p for p in form.parts if p.block == "cards"), None)
    kpi = next((p for p in form.parts if p.block == "kpi"), None)
    quote = next((p for p in form.parts if p.block == "quote"), None)
    text_places = [
        s for s in pattern.slots
        if s.role in _TEXT_PLACE_ROLES and not keeps_sample_text(s) and getattr(s, "max_chars", 0) > 0
    ]
    required_text = sum(1 for p in form.parts if p.required and p.block in ("text", "bullets"))
    if repeat is None:
        topology = "none"
    elif getattr(repeat, "rows", 1) > 1 and getattr(repeat, "cols", 0) > 1:
        topology = "grid"
    else:
        topology = "row" if getattr(repeat, "axis", "x") == "x" else "column"
    decor = getattr(pattern, "decor", None) or []
    unit_decor = any(getattr(d, "repeat_group", False) for d in decor) if repeat is not None else False
    words = sum(p.units * p.unit.max_words for p in form.parts)
    return PatternCapabilities(
        headline=form.headline is not None,
        text_blocks_min=required_text,
        text_blocks_max=len(text_places),
        card_count_min=2 if cards is not None else 0,
        card_count_max=cards.units if cards is not None else 0,
        card_has_title=bool(cards is not None and cards.title_slot),
        card_has_body=cards is not None,
        card_has_icon=bool(cards is not None and (unit_decor or "icon" in repeat_roles)),
        card_has_image=bool(cards is not None and "image" in repeat_roles),
        supports_table=form.has_table,
        # Место под график по ступеням задачи V1 (родной график, картинка-
        # график примера, крупное текстовое место), а не любая картинка:
        # фото-рамка графику не место.
        supports_chart=form.chart_tier is not None or form.has_chart or form.has_table,
        supports_photo=form.has_image,
        supports_quote=quote is not None,
        supports_kpi=kpi is not None,
        kpi_count_min=1 if kpi is not None else 0,
        kpi_count_max=kpi.units if kpi is not None else 0,
        repeat_topology=topology,
        sequence_semantics=bool(repeat is not None and (unit_decor or "kpi_value" in repeat_roles)),
        max_total_words=int(words),
        photo_frames=int(getattr(pattern, "photo_frames", 0) or 0),
        photo_area=float(getattr(pattern, "photo_area", 0.0) or 0.0),
        photo_slot_area=float(getattr(pattern, "photo_slot_area", 0.0) or 0.0),
    )


@dataclass(frozen=True)
class SlideRequirements:
    """Что слайду нужно от раскладки. До письма (`from_intent`) известны
    заказанная форма и фото; после письма (`from_slide`) блоки, которые
    написал писатель. `allow_split`: карточек больше мест, но лестница
    сборки может разделить слайд на два."""
    headline: bool = True
    cards: int = 0
    bullets: int = 0
    text_blocks: int = 0
    kpis: int = 0
    table: bool = False
    chart: bool = False
    photo: bool = False
    quote: bool = False
    allow_split: bool = True
    max_photo_void: float = MAX_PHOTO_VOID

    @classmethod
    def from_intent(cls, intent) -> "SlideRequirements":
        form = getattr(intent, "form", None)
        return cls(
            headline=True,
            kpis=int(getattr(intent, "items", 0) or 0) if form == "kpi" else 0,
            table=form == "table", chart=form == "chart", quote=form == "quote",
            photo=bool(getattr(intent, "photo", None)),
        )

    @classmethod
    def from_slide(cls, slide) -> "SlideRequirements":
        from deckforge.plan.spec import BulletBlock, CardBlock, KpiBlock, QuoteBlock, TextBlock

        blocks = list(slide.blocks)
        visual = slide.visual
        return cls(
            headline=bool((slide.headline or "").strip()),
            cards=sum(len(b.items) for b in blocks if isinstance(b, CardBlock)),
            bullets=sum(len(b.items) for b in blocks if isinstance(b, BulletBlock)),
            text_blocks=sum(1 for b in blocks if isinstance(b, TextBlock) and b.text.strip()),
            kpis=sum(len(b.items) for b in blocks if isinstance(b, KpiBlock)),
            quote=any(isinstance(b, QuoteBlock) and b.text.strip() for b in blocks),
            table=visual is not None and visual.kind == "table",
            chart=visual is not None and visual.kind == "chart",
            photo=visual is not None and visual.kind == "photo",
        )


def unmet_requirements(req: SlideRequirements, caps: PatternCapabilities) -> list[str]:
    """Требования, которых раскладка не держит, словами. Пустой список:
    требования входят в возможности."""
    missing: list[str] = []
    if req.headline and not caps.headline:
        missing.append("нет места под заголовок")
    if req.cards:
        need = 2 if req.allow_split else req.cards
        if caps.card_count_max < need:
            missing.append(f"карточек {req.cards}, мест под карточки {caps.card_count_max}")
    if req.bullets and not (caps.text_blocks_max or caps.card_count_max):
        missing.append("нет места под список")
    if req.text_blocks and not (caps.text_blocks_max or caps.card_count_max or caps.supports_quote):
        missing.append("нет места под абзац")
    if req.kpis and not caps.supports_kpi:
        missing.append("нет места под показатели")
    if req.table and not caps.supports_table:
        missing.append("нет места под таблицу")
    if req.chart and not caps.supports_chart:
        missing.append("нет места под график")
    if req.photo and not caps.supports_photo:
        missing.append("нет места под фото")
    if req.quote and not (caps.supports_quote or caps.text_blocks_max):
        missing.append("нет места под цитату")
    void = caps.photo_void(req.photo)
    if void > req.max_photo_void:
        missing.append(f"после удаления фото примера пустеет {void:.0%} холста")
    return missing
