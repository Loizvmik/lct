"""Укладчики содержания плана (`deckforge.plan.spec`) в слоты раскладки
(`Pattern.slots`) — по РОЛИ слота, не по порядку: `headline` всегда идёт в
слот с ролью `"headline"`, буллеты — в слот с ролью `"bullet"`, и т.д. (см.
`ROLES` в `template/patterns.py`).

Модуль не рисует ничего на `.pptx` — только решает, КАКОЙ слот получит
КАКОЙ текст (и, для `CardBlock`/`KpiBlock`, сколько экземпляров повторить и
где). Собственно измерение (влезает ли), ужимание по ступеням шкалы и
рисование через python-pptx — работа `builder.py`, единственного места,
которое трогает и `textfit`, и python-pptx одновременно.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field, replace

from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.plan.spec import (
    Block, BulletBlock, Card, CardBlock, DeckSpec, Kpi, KpiBlock, QuoteBlock, SlideSpec, TextBlock,
)
from deckforge.template.grid import Grid
from deckforge.template.patterns import Pattern, PatternSlot

_DEFAULT_BULLET_CHAR = "•"


@dataclass
class Paragraph:
    text: str
    bullet: bool = False


@dataclass
class SlotContent:
    """Один слот раскладки вместе с тем, что в него ляжет — единица, которую
    строит этот модуль и потребляет `builder.place_slide` (замер/ужимание/
    отрисовка)."""
    slot: PatternSlot
    role_hint: str
    paragraphs: list[Paragraph] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Распределение содержания по слотам
# ---------------------------------------------------------------------------


def assign_content(slide_spec: SlideSpec, pattern: Pattern, grid: Grid) -> list[SlotContent]:
    """Раскладывает `slide_spec` по слотам `pattern` — контент, для которого
    в этой раскладке не нашлось подходящего по роли слота, просто не
    попадает в результат (не ошибка сама по себе: находки о том, что
    контент НЕ ВЛЕЗ по РАЗМЕРУ, а не по отсутствию слота, пишет
    `builder.py` при замере — здесь только соответствие ролей)."""
    by_role = _slots_by_role(pattern, slide_spec)
    result: list[SlotContent] = []

    headline_slot = _take_one(by_role, "headline")
    if headline_slot is not None and slide_spec.headline:
        result.append(SlotContent(headline_slot, "headline", [Paragraph(slide_spec.headline)]))

    subhead_slot = _take_one(by_role, "subhead") or _take_one(by_role, "caption")
    if subhead_slot is not None and slide_spec.subhead:
        result.append(SlotContent(subhead_slot, "subhead", [Paragraph(slide_spec.subhead)]))

    for block in slide_spec.blocks:
        result.extend(_assign_block(block, by_role, pattern, grid))

    source_slot = _take_one(by_role, "source") or _take_one(by_role, "caption")
    if source_slot is not None and slide_spec.source_note:
        result.append(SlotContent(source_slot, "source", [Paragraph(slide_spec.source_note)]))

    return result


def _slots_by_role(pattern: Pattern, slide_spec: SlideSpec) -> dict[str, list[PatternSlot]]:
    """Слоты сгруппированы по роли. Роли, входящие в `pattern.repeat`
    (`card_title`/`card_body`/... повтора), исключаются из прямой раздачи
    ТОЛЬКО когда в `slide_spec` реально есть `CardBlock`/`KpiBlock`,
    претендующий их развернуть через `expand_repeat` (см. `_assign_cards`/
    `_assign_kpis`) — иначе повторяющиеся слоты (например, пара `card_body`
    у "bullets"-раскладки с repeat count=2, не дотянувшим до классификации
    "cards", см. `patterns._classify_kind`) остались бы недоступны вовсе
    единственному текстовому блоку слайда, у которого просто нет своего
    формального слота "bullet"/"body"."""
    reserved_for_repeat = any(isinstance(b, (CardBlock, KpiBlock)) for b in slide_spec.blocks)
    repeat_roles = set(pattern.repeat.slot_roles) if (pattern.repeat is not None and reserved_for_repeat) else set()
    by_role: dict[str, list[PatternSlot]] = {}
    for slot in pattern.slots:
        if slot.role in repeat_roles:
            continue
        by_role.setdefault(slot.role, []).append(slot)
    return by_role


def _take_one(by_role: dict[str, list[PatternSlot]], role: str) -> PatternSlot | None:
    items = by_role.get(role)
    if not items:
        return None
    return items.pop(0)


def _assign_block(
    block: Block, by_role: dict[str, list[PatternSlot]], pattern: Pattern, grid: Grid,
) -> list[SlotContent]:
    if isinstance(block, TextBlock):
        # "card_body" — запасной вариант: во многих намайненных "bullets"-
        # раскладках основной текст сидит в свободных фигурах с ролью
        # card_body (не в формальном плейсхолдере "body" — см. докстроку
        # `mine_patterns`: "плейсхолдеров в шаблонах почти нет"), это тот же
        # по смыслу связный текст, просто найденный структурно иначе.
        slot = _take_one(by_role, "body") or _take_one(by_role, "card_body")
        return [SlotContent(slot, "body", [Paragraph(block.text)])] if slot is not None else []

    if isinstance(block, BulletBlock):
        slot = _take_one(by_role, "bullet") or _take_one(by_role, "body") or _take_one(by_role, "card_body")
        if slot is None:
            return []
        return [SlotContent(slot, "bullets", [Paragraph(item, bullet=True) for item in block.items])]

    if isinstance(block, QuoteBlock):
        return _assign_quote(block, by_role)

    if isinstance(block, CardBlock):
        return _assign_cards(block, pattern, grid)

    if isinstance(block, KpiBlock):
        return _assign_kpis(block, by_role)

    return []


def _assign_quote(block: QuoteBlock, by_role: dict[str, list[PatternSlot]]) -> list[SlotContent]:
    slot = _take_one(by_role, "quote") or _take_one(by_role, "body") or _take_one(by_role, "card_body")
    if slot is None:
        return []
    result = [SlotContent(slot, "quote", [Paragraph(block.text)])]
    if block.author:
        author_slot = _take_one(by_role, "caption") or _take_one(by_role, "source")
        if author_slot is not None:
            result.append(SlotContent(author_slot, "quote_author", [Paragraph(f"— {block.author}")]))
    return result


def _assign_cards(block: CardBlock, pattern: Pattern, grid: Grid) -> list[SlotContent]:
    if pattern.repeat is None or not block.items:
        return []
    groups = expand_repeat(pattern, len(block.items), grid)
    result: list[SlotContent] = []
    for card, group in zip(block.items, groups):
        for slot in group:
            if slot.role == "card_title":
                if card.title:
                    result.append(SlotContent(slot, "card_title", [Paragraph(card.title)]))
            elif slot.role in ("card_body", "bullet", "body"):
                result.append(SlotContent(slot, "card_body", [Paragraph(card.body)]))
    return result


def _assign_kpis(block: KpiBlock, by_role: dict[str, list[PatternSlot]]) -> list[SlotContent]:
    value_slots = by_role.get("kpi_value", [])
    label_slots = by_role.get("kpi_label", [])
    n = min(len(block.items), len(value_slots))
    result: list[SlotContent] = []
    for i in range(n):
        item = block.items[i]
        result.append(SlotContent(value_slots[i], "kpi_value", [Paragraph(item.value)]))
        if i < len(label_slots):
            result.append(SlotContent(label_slots[i], "kpi_label", [Paragraph(item.label)]))
    del value_slots[:n]
    del label_slots[:min(n, len(label_slots))]
    return result


# ---------------------------------------------------------------------------
# Развёртка повтора под фактическое число элементов
# ---------------------------------------------------------------------------


def expand_repeat(pattern: Pattern, n: int, grid: Grid) -> list[list[PatternSlot]]:
    """Разворачивает `pattern.repeat` под РЕАЛЬНОЕ число элементов `n`, а не
    под то, сколько карточек было намайнено со слайда-примера (бриф:
    "раскладка снята с шести карточек, а положить надо четыре или восемь").

    Геометрия одной единицы повтора (размер по оси, положение по другой
    оси) берётся с ПЕРВОЙ намайненной группы — эталон формы карточки/строки
    этой раскладки. Шаг ПЕРЕСЧИТЫВАЕТСЯ (не берётся `repeat.step` как есть)
    так, чтобы `n` единиц заняли весь доступный интервал между полями
    шаблона РАВНОМЕРНО: `new_step = (span - item_size) / (n - 1)` — при
    `n`, отличном от намайненного `repeat.count`, старый шаг либо оставил
    бы пустоты, либо не поместил бы все элементы; калибровка "равномерно
    через весь span" не выведена из трёх учебных файлов, а единственный
    геометрически осмысленный способ распределить произвольное `n` между
    двумя фиксированными полями."""
    repeat = pattern.repeat
    if repeat is None or n <= 0:
        return []
    axis = repeat.axis
    members = [s for s in pattern.slots if s.role in repeat.slot_roles]
    if not members:
        return []

    def axis_of(box: Box) -> float:
        return box.left if axis == "x" else box.top

    def with_axis(box: Box, value: float) -> Box:
        return replace(box, left=value) if axis == "x" else replace(box, top=value)

    def size_of(box: Box) -> float:
        return box.width if axis == "x" else box.height

    groups: dict[float, list[PatternSlot]] = {}
    for slot in members:
        key = round(axis_of(slot.box), 3)
        groups.setdefault(key, []).append(slot)
    ordered = [groups[k] for k in sorted(groups)]
    template_group = ordered[0]
    # Размер ЕДИНИЦЫ повтора вдоль оси — наибольший размер среди слотов
    # группы, а не размер первого попавшегося (находка ручной проверки на
    # шаблоне VK Education: группа "карточка+KPI" несёт узкий kpi_value и
    # широкий card_body на ОДНОЙ x-координате — они стоят друг над другом,
    # а не рядом, и вся единица повтора занимает по ширине ровно столько,
    # сколько самый широкий из её слотов, не столько, сколько первый по
    # порядку `pattern.slots`. С узким размером шаг пересчитывался с
    # завышенным "свободным" пространством и последняя карточка уезжала за
    # правое поле холста.
    item_size = max(size_of(s.box) for s in template_group)

    margin_lo = grid.margin_left if axis == "x" else grid.margin_top
    margin_hi = grid.margin_right if axis == "x" else grid.margin_bottom
    span = max(1.0 - margin_lo - margin_hi, 0.0)
    usable = max(span - item_size, 0.0)
    new_step = usable / (n - 1) if n > 1 else 0.0

    result: list[list[PatternSlot]] = []
    for i in range(n):
        pos = margin_lo + i * new_step
        result.append([replace(slot, box=with_axis(slot.box, pos)) for slot in template_group])
    return result


# ---------------------------------------------------------------------------
# Буллет шаблона
# ---------------------------------------------------------------------------

_TEXT_PART_PREFIXES = ("ppt/slides/", "ppt/slideLayouts/", "ppt/slideMasters/")


def find_bullet_char(pkg: PptxPackage) -> str:
    """Буллет-символ, которым шаблон реально маркирует списки — мода
    `a:buChar/@char` по всему пакету (слайды, лейауты, мастера), не точка
    наугад (бриф: "буллет берётся из шаблона, а не ставится точкой
    наугад"). `"•"` — единственный запасной вариант, когда в шаблоне вовсе
    нет ни одного явного `a:buChar`: это не произвольный выбор кода, а
    собственный дефолт формата OOXML, который применяет сам PowerPoint,
    когда список не переопределяет буллет явно."""
    votes: Counter[str] = Counter()
    for part in pkg.names():
        if not part.endswith(".xml") or not part.startswith(_TEXT_PART_PREFIXES):
            continue
        root = pkg.xml(part)
        for buchar in root.iter(qn("a:buChar")):
            char = buchar.get("char")
            if char:
                votes[char] += 1
    return votes.most_common(1)[0][0] if votes else _DEFAULT_BULLET_CHAR
