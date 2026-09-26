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

from deckforge.template.patterns import CHARS_PER_WORD, keeps_sample_text, slot_char_capacity

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
    )


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
