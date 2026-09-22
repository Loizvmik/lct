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
from deckforge.template.patterns import DecorShape, Pattern, PatternSlot

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
    ТОЛЬКО когда в `slide_spec` реально есть `CardBlock`, претендующий их
    развернуть через `expand_repeat` (см. `_assign_cards`) — иначе
    повторяющиеся слоты (например, пара `card_body` у "bullets"-раскладки с
    repeat count=2, не дотянувшим до классификации "cards", см.
    `patterns._classify_kind`) остались бы недоступны вовсе единственному
    текстовому блоку слайда, у которого просто нет своего формального слота
    "bullet"/"body".

    `KpiBlock` (Task 13, находка обязательной проверки — живой прогон на
    queue-latency) НАМЕРЕННО не резервирует эти роли, хотя раньше резервировал
    наравне с `CardBlock`: `_assign_kpis` не вызывает `expand_repeat` вовсе
    (она читает `kpi_value`/`kpi_label` напрямую из `by_role`), а её
    запасной вариант для раскладки БЕЗ единого kpi-слота (VK Tech: 0 из 20
    раскладок kind="kpi") как раз метит в "bullet"/"body"/"card_body" — были
    они зарезервированы, KPI-метрики на не-kpi раскладке молча теряли ВСЕ
    свои факты (слайд с одним заголовком и без единой цифры, живой рендер)."""
    reserved_for_repeat = any(isinstance(b, CardBlock) for b in slide_spec.blocks)
    repeat_roles = set(pattern.repeat.slot_roles) if (pattern.repeat is not None and reserved_for_repeat) else set()
    by_role: dict[str, list[PatternSlot]] = {}
    for slot in pattern.slots:
        if slot.role in repeat_roles:
            continue
        by_role.setdefault(slot.role, []).append(slot)
    # Task 9 повторное ревью, находка №2 ("важное"): слоты одной роли не
    # были упорядочены по размеру — `_take_one` (см. ниже) брал ПЕРВЫЙ по
    # порядку мининга слот этой роли, независимо от того, влезает ли в него
    # содержание. На контрольном ЛЦТ2026 (слайд "two_col") это укладывало
    # абзац в крошечный слот-подпись у нижнего правого угла, который тот же
    # паттерн несёт наравне с нормальным слотом "body" — текст резался
    # краем холста. Сортировка по убыванию площади (тот же приём, что уже
    # применяет `_pick_body_slot` внутри группы повтора карточек, см. ниже)
    # гарантирует, что `_take_one` вернёт САМЫЙ ЁМКИЙ слот этой роли первым
    # — содержание кладётся в тот, что вмещает, а не в первый попавшийся.
    for slots in by_role.values():
        slots.sort(key=lambda s: -(s.box.width * s.box.height))
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


_CARD_BODY_ROLES = ("card_body", "bullet", "body")


def _assign_cards(block: CardBlock, pattern: Pattern, grid: Grid) -> list[SlotContent]:
    if pattern.repeat is None or not block.items:
        return []
    groups = expand_repeat(pattern, len(block.items), grid)
    result: list[SlotContent] = []
    for card, group in zip(block.items, groups):
        body_slot = _pick_body_slot(group)
        for slot in group:
            if slot.role == "card_title":
                if card.title:
                    result.append(SlotContent(slot, "card_title", [Paragraph(card.title)]))
            elif slot is body_slot:
                result.append(SlotContent(slot, "card_body", [Paragraph(card.body)]))
    return result


def _pick_body_slot(group: list[PatternSlot]) -> PatternSlot | None:
    """Когда в ОДНОЙ группе повтора несколько слотов роли
    `"card_body"`/`"bullet"`/`"body"` (майнинг не всегда структурно отличает
    заголовок-подпись от абзаца — находка ручной проверки Task 9, отчёт
    задачи: паттерн "slide14" шаблона VK Tech несёт узкий `card_body`
    (сэмпл-текст "01"/"02", высота 0.032 холста — место под номер, не под
    предложение) и широкий `card_body` (место под абзац) в одной группе),
    текст карточки кладётся ТОЛЬКО в САМЫЙ ЁМКИЙ по площади слот этой роли.

    Раньше (до этой правки) `_assign_cards` клал ОДИН И ТОТ ЖЕ текст
    карточки во ВСЕ такие слоты группы — узкий слот получал полноценное
    предложение, не помещался ни на одном кегле шкалы и усекался до
    голого «…» (see `builder._is_cosmetic_truncation`) — пустая/усечённая-
    до-точек карточка хуже, чем оставить узкий слот вовсе без текста."""
    candidates = [s for s in group if s.role in _CARD_BODY_ROLES]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.box.width * s.box.height)


def _assign_kpis(block: KpiBlock, by_role: dict[str, list[PatternSlot]]) -> list[SlotContent]:
    # Task 13, находка обязательной проверки (визуальный ревью на VK
    # Education): раскладки `kind="kpi"` этого шаблона несут ГРИД метрик
    # (2 строки по 4 столбца) — все `kpi_value`/все `kpi_label` слоты
    # одного грида приходят одинакового размера, поэтому сортировка
    # `_slots_by_role` по убыванию площади (см. её докстроку) — НЕ-ОП на
    # равных размерах и оставляет слоты в порядке ОБХОДА XML слайда-примера,
    # который не обязан совпадать со зрительным порядком "слева направо,
    # сверху вниз". Спаривание `value_slots[i]`/`label_slots[i]` ПО ИНДЕКСУ
    # без этого требует, чтобы оба списка уже шли в ОДНОМ геометрическом
    # порядке — иначе значение одной колонки визуально спаривается с
    # подписью совсем другой (живой рендер: "−80%" встал слева, а его
    # подпись "медиана" — у подписи четвёртой колонки, за экраном друг от
    # друга). Сортировка обоих списков по (top, left) ПЕРЕД спариванием
    # восстанавливает зрительный порядок независимо от порядка обхода XML —
    # `kpi_label` слот сидит НИЖЕ своего `kpi_value` при той же `left`
    # (замер VK Education: value left=0.055 top=0.26, label left=0.055
    # top=0.422 — совпадают по `left`, различаются по `top`), поэтому
    # сортировка "сверху вниз, слева направо" у обоих ролей даёт ОДИНАКОВЫЙ
    # порядок столбцов на каждой строке.
    value_slots = by_role.get("kpi_value", [])
    label_slots = by_role.get("kpi_label", [])
    # Сортировка IN PLACE (не `sorted()` в новый список) — `del ...[:n]`
    # ниже обязан продолжать мутировать те же списки, что лежат в `by_role`
    # (контракт `_slots_by_role`/`_take_one`: слот, отданный одному блоку,
    # не должен снова достаться другому, см. их докстроки).
    value_slots.sort(key=lambda s: (round(s.box.top, 3), s.box.left))
    label_slots.sort(key=lambda s: (round(s.box.top, 3), s.box.left))
    n = min(len(block.items), len(value_slots))
    result: list[SlotContent] = []
    for i in range(n):
        item = block.items[i]
        result.append(SlotContent(value_slots[i], "kpi_value", [Paragraph(item.value)]))
        if i < len(label_slots):
            result.append(SlotContent(label_slots[i], "kpi_label", [Paragraph(item.label)]))
    del value_slots[:n]
    del label_slots[:min(n, len(label_slots))]

    # Task 13, находка обязательной проверки (живой прогон на queue-latency,
    # VK Tech): раскладку `kind="kpi"` шаблон может не нести вовсе (VK Tech
    # — 0 раскладок этого kind из 20), а `plan.variants.apply_variant`
    # обязан всё равно выбрать КАКУЮ-ТО раскладку (`kind_pool` деградирует
    # на любой доступный `kind` варианта, а не оставляет слайд несобранным)
    # — не-kpi раскладка не несёт `kpi_value`/`kpi_label` вовсе, и n=0
    # оставляло KpiBlock ПОЛНОСТЬЮ потерянным (слайд с одним заголовком без
    # единой цифры — находка: пустые карточки на живом рендере, ни один
    # факт не попал на слайд). Запасной вариант — тот же приём, что уже
    # применяют `_assign_block` для BulletBlock ("card_body" как запасная
    # роль) и `_assign_quote` для QuoteBlock без роли "quote": метрики, не
    # уместившиеся (или не уместившиеся вовсе) в kpi-слоты, рендерятся
    # строкой "значение — подпись" в обычном текстовом слоте — хуже
    # выделенного KPI-блока визуально, но не теряет ни один факт молча.
    remaining = block.items[n:]
    if remaining:
        fallback_slot = _take_one(by_role, "bullet") or _take_one(by_role, "body") or _take_one(by_role, "card_body")
        if fallback_slot is not None:
            lines = [f"{item.value} — {item.label}" if item.label else item.value for item in remaining]
            result.append(SlotContent(fallback_slot, "bullets", [Paragraph(line, bullet=True) for line in lines]))

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
    positions = _expand_positions(axis, item_size, n, grid, repeat.step)

    result: list[list[PatternSlot]] = []
    for pos in positions:
        result.append([replace(slot, box=with_axis(slot.box, pos)) for slot in template_group])
    return result


def _expand_positions(axis: str, item_size: float, n: int, grid: Grid, native_step: float) -> list[float]:
    """Позиции (координата вдоль `axis`, доли холста) `n` единиц размера
    `item_size` между полями шаблона — геометрическое ядро, общее для
    `expand_repeat` (текстовые слоты) и `expand_decor` (декор группы
    повтора, см. ниже), чтобы декор пересчитывался ТЕМ ЖЕ шагом, что и
    текст, а не отдельной, потенциально разъезжающейся копией формулы.

    Task 10 отчёт, находка №3 ("шаг растянулся"): раньше шаг ВСЕГДА
    пересчитывался так, чтобы `n` единиц заняли весь `span` между полями
    (`new_step = (span - item_size) / (n - 1)`), независимо от того,
    сколько элементов было намайнено. При уменьшении числа элементов
    (раскладка на 3 карточки, контента — 2) это растягивало интервал на
    всю ширину слайда: два текста расходились к противоположным краям, а
    визуально ряд карточек переставал читаться как ряд (VK Tech, "Риски
    раскатки"). Теперь ШАГ ПО УМОЛЧАНИЮ — РОДНОЙ, намайненный
    (`repeat.step`, `native_step`): `n` единиц ставятся КОМПАКТНО, встык
    друг к другу тем же шагом, что и в раскладке-источнике, ВЫРОВНЕННЫМИ
    ПО НАЧАЛУ контентной области (`margin_lo`) — тот же угол, с которого
    начиналась исходная группа. Растягивающий пересчёт остаётся ЗАПАСНЫМ
    вариантом ТОЛЬКО когда компактная раскладка физически не помещается
    между полями при родном шаге (`n` заметно БОЛЬШЕ намайненного
    `repeat.count`, единственный случай, где растягивать необходимо,
    чтобы вообще всё поместилось)."""
    margin_lo = grid.margin_left if axis == "x" else grid.margin_top
    margin_hi = grid.margin_right if axis == "x" else grid.margin_bottom
    span = max(1.0 - margin_lo - margin_hi, 0.0)
    step = native_step if native_step and native_step > 0 else 0.0
    compact_width = item_size + step * (n - 1) if n > 1 else item_size
    if n <= 1 or compact_width <= span + 1e-9:
        return [margin_lo + i * step for i in range(n)]
    usable = max(span - item_size, 0.0)
    fallback_step = usable / (n - 1) if n > 1 else 0.0
    return [margin_lo + i * fallback_step for i in range(n)]


def expand_decor(pattern: Pattern, n: int | None, grid: Grid) -> list[DecorShape]:
    """Разворачивает декор ГРУППЫ ПОВТОРА (`DecorShape.repeat_group`, см.
    `template/patterns.py::_decor_repeat_membership`) под фактическое число
    элементов `n` — ВМЕСТЕ с текстовыми слотами (`expand_repeat`) и ТЕМ ЖЕ
    пересчётом шага (`_expand_positions`, общее геометрическое ядро с
    `expand_repeat` выше): раскладка, снятая с шести декоративных рамок,
    под два элемента содержания несёт на слайде ДВЕ рамки, не шесть —
    лишние повторы (намайненный `RepeatSpec.count` больше `n`) не
    рисуются вовсе (Task 9 повторное ревью, находка №1, "главная находка":
    VK Tech, "Риски раскатки" — шесть намайненных рамок, два элемента
    содержания, четыре оставались пустыми).

    Единица повтора (размер по оси) берётся с САМОЙ ПЕРВОЙ по оси
    намайненной группы декора (`repeat_index == min(...)`), тот же приём,
    что `expand_repeat` использует для текстовых слотов (эталон формы
    карточки/плашки этой раскладки).

    Декор ВНЕ группы повтора (`repeat_group=False` — логотип,
    разделительная линия, самостоятельная плашка) переносится КАК ЕСТЬ, без
    изменений: он не связан с числом элементов содержания.

    `n=None` — на слайде нет содержания, которое разворачивает
    `pattern.repeat` (сегодня это только `CardBlock` с непустыми `items`,
    единственный вызывающий `expand_repeat`, см. `_assign_cards`) — декор
    остаётся как намайнен, менять его не под что."""
    grouped = [d for d in pattern.decor if d.repeat_group]
    ungrouped = [d for d in pattern.decor if not d.repeat_group]
    if not grouped or pattern.repeat is None or n is None:
        return list(pattern.decor)
    if n <= 0:
        return ungrouped

    repeat = pattern.repeat
    axis = repeat.axis

    def with_axis(box: Box, value: float) -> Box:
        return replace(box, left=value) if axis == "x" else replace(box, top=value)

    def size_of(box: Box) -> float:
        return box.width if axis == "x" else box.height

    by_index: dict[int, list[DecorShape]] = {}
    for d in grouped:
        by_index.setdefault(d.repeat_index, []).append(d)
    template_group = by_index[min(by_index)]
    item_size = max(size_of(d.box) for d in template_group)
    positions = _expand_positions(axis, item_size, n, grid, repeat.step)

    result = list(ungrouped)
    for i, pos in enumerate(positions):
        for d in template_group:
            result.append(replace(d, box=with_axis(d.box, pos), repeat_index=i))
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
