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


@dataclass(frozen=True)
class DroppedContent:
    """Кусок содержания плана, которому в ЭТОЙ раскладке не нашлось слота
    подходящей роли, — он не попадёт на слайд вовсе.

    Само по себе это не ошибка сборки (раскладка честно может не нести
    слот под подзаголовок или сноску, и слайд всё равно собирается), но
    МОЛЧАНИЕМ это быть не должно: расхождение между тем, что написала
    модель, и тем, что оказалось в файле, обязана называть наша же
    проверка качества, а не глаз человека, сверяющего .json с .pptx.
    Раньше (до этой правки) такой текст исчезал бесследно — ни находки,
    ни строки в отчёте (живой прогон контрольного шаблона: подзаголовок
    "Рост доведения до 57%, стоимость выпускника −44%" с цифрами из брифа
    не попал ни в один слот раскладки `two_col` и нигде не был упомянут).

    `role` — роль содержания в терминах `ROLES`/`role_hint` ("subhead",
    "source", "card_title", ...), `text` — что именно потерялось (первых
    слов достаточно, чтобы человек нашёл это место в плане).

    Собирает их `assign_content_with_drops`, превращает в находки
    `slide_spec.findings` — `builder.place_slide`, ЕДИНСТВЕННОЕ место
    реальной укладки: `assign_content` зовётся и для РАНЖИРОВАНИЯ
    кандидатов (`builder.fits`), там находки писать не во что и незачем —
    раскладка ещё не выбрана."""
    role: str
    text: str


# Человеческие названия ролей для находки — рядом с самими ролями, а не в
# `builder.py`: кто заводит роль, тот и отвечает за то, как она называется
# в отчёте, который читает человек.
DROPPED_ROLE_TITLES: dict[str, str] = {
    "headline": "заголовок",
    "subhead": "подзаголовок",
    "source": "сноска об источнике",
    "body": "текстовый блок",
    "bullets": "список",
    "quote": "цитата",
    "quote_author": "автор цитаты",
    "cards": "карточки",
    "card_title": "заголовки карточек",
    "card_body": "тексты карточек",
    "kpi": "метрики",
}

# Сколько знаков потерянного текста показывать в находке: достаточно,
# чтобы человек узнал фразу и нашёл её в плане, и мало, чтобы отчёт
# остался читаемым (абзац карточки бывает длиннее самой находки).
_DROPPED_EXCERPT_CHARS = 60


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _DROPPED_EXCERPT_CHARS:
        return flat
    return flat[:_DROPPED_EXCERPT_CHARS].rstrip() + "…"


def _drop(drops: list[DroppedContent], role: str, *parts: str) -> None:
    """Запоминает потерю — ТОЛЬКО если терять было что: у блока может
    честно не быть содержания (пустой список карточек, метрика без
    подписи), и находка о пустоте — шум, а не находка."""
    text = "; ".join(p.strip() for p in parts if p and p.strip())
    if text:
        drops.append(DroppedContent(role, _excerpt(text)))


# ---------------------------------------------------------------------------
# Распределение содержания по слотам
# ---------------------------------------------------------------------------


def assign_content(slide_spec: SlideSpec, pattern: Pattern, grid: Grid) -> list[SlotContent]:
    """Раскладывает `slide_spec` по слотам `pattern` — см.
    `assign_content_with_drops`, эта обёртка нужна тем, кому интересна
    только сама укладка (ранжирование кандидатов `builder.fits`), а не
    список потерянного."""
    return assign_content_with_drops(slide_spec, pattern, grid)[0]


def assign_content_with_drops(
    slide_spec: SlideSpec, pattern: Pattern, grid: Grid,
) -> tuple[list[SlotContent], list[DroppedContent]]:
    """Раскладывает `slide_spec` по слотам `pattern` и ВТОРЫМ значением
    возвращает то, что в эту раскладку не поместилось ПО РОЛЯМ: контент,
    для которого здесь нет подходящего слота, на слайд не попадает (не
    ошибка сборки сама по себе — слайд собирается как собирался), но и не
    исчезает молча (см. докстроку `DroppedContent`).

    Находки о том, что контент не влез по РАЗМЕРУ, а не по отсутствию
    слота, по-прежнему пишет `builder.py` при замере — здесь только
    соответствие ролей."""
    by_role = _slots_by_role(pattern, slide_spec)
    result: list[SlotContent] = []
    drops: list[DroppedContent] = []

    headline_slot = _take_one(by_role, "headline")
    if slide_spec.headline:
        if headline_slot is not None:
            result.append(SlotContent(headline_slot, "headline", [Paragraph(slide_spec.headline)]))
        else:
            _drop(drops, "headline", slide_spec.headline)

    subhead_slot = _take_one(by_role, "subhead") or _take_one(by_role, "caption")
    if slide_spec.subhead:
        if subhead_slot is not None:
            result.append(SlotContent(subhead_slot, "subhead", [Paragraph(slide_spec.subhead)]))
        else:
            _drop(drops, "subhead", slide_spec.subhead)

    for block in slide_spec.blocks:
        result.extend(_assign_block(block, by_role, pattern, grid, drops))

    source_slot = _take_one(by_role, "source") or _take_one(by_role, "caption")
    if slide_spec.source_note:
        if source_slot is not None:
            result.append(SlotContent(source_slot, "source", [Paragraph(slide_spec.source_note)]))
        else:
            _drop(drops, "source", slide_spec.source_note)

    return result, drops


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
    drops: list[DroppedContent],
) -> list[SlotContent]:
    if isinstance(block, TextBlock):
        # "card_body" — запасной вариант: во многих намайненных "bullets"-
        # раскладках основной текст сидит в свободных фигурах с ролью
        # card_body (не в формальном плейсхолдере "body" — см. докстроку
        # `mine_patterns`: "плейсхолдеров в шаблонах почти нет"), это тот же
        # по смыслу связный текст, просто найденный структурно иначе.
        slot = _take_one(by_role, "body") or _take_one(by_role, "card_body")
        if slot is None:
            _drop(drops, "body", block.text)
            return []
        return [SlotContent(slot, "body", [Paragraph(block.text)])]

    if isinstance(block, BulletBlock):
        spread = _spread_bullets_over_repeat(block, by_role, pattern, grid)
        if spread is not None:
            return spread
        slot = _take_one(by_role, "bullet") or _take_one(by_role, "body") or _take_one(by_role, "card_body")
        if slot is None:
            _drop(drops, "bullets", *block.items)
            return []
        return [SlotContent(slot, "bullets", [Paragraph(item, bullet=True) for item in block.items])]

    if isinstance(block, QuoteBlock):
        return _assign_quote(block, by_role, drops)

    if isinstance(block, CardBlock):
        return _assign_cards(block, pattern, grid, drops)

    if isinstance(block, KpiBlock):
        return _assign_kpis(block, by_role, drops)

    return []


def _assign_quote(
    block: QuoteBlock, by_role: dict[str, list[PatternSlot]], drops: list[DroppedContent],
) -> list[SlotContent]:
    slot = _take_one(by_role, "quote") or _take_one(by_role, "body") or _take_one(by_role, "card_body")
    if slot is None:
        _drop(drops, "quote", block.text, block.author or "")
        return []
    result = [SlotContent(slot, "quote", [Paragraph(block.text)])]
    if block.author:
        author_slot = _take_one(by_role, "caption") or _take_one(by_role, "source")
        if author_slot is not None:
            result.append(SlotContent(author_slot, "quote_author", [Paragraph(f"— {block.author}")]))
        else:
            _drop(drops, "quote_author", block.author)
    return result


_CARD_BODY_ROLES = ("card_body", "bullet", "body")


def _spread_bullets_over_repeat(
    block: BulletBlock, by_role: dict[str, list[PatternSlot]], pattern: Pattern, grid: Grid,
) -> list[SlotContent] | None:
    """Раскладывает пункты списка ПО ЕДИНИЦАМ ПОВТОРА раскладки — по пункту
    в каждую, — если раскладка построена на повторе. `None` означает «эта
    раскладка не про повтор», и список кладётся в один блок, как раньше.

    Зачем. Декор шаблона прибит к структуре: три иконки существуют потому,
    что под ними три подписи. Пока список падал в ОДИН текстовый блок,
    украшенная раскладка либо оставалась пустой (единицы повтора не
    заполнены — декор не рисуется), либо давала осиротевшие иконки без
    подписей. Живой рендер 25 сентября 2026 на VK Education: одиннадцать
    слайдов из двенадцати вышли белыми листами, при том что фирменная
    графика в шаблоне есть.

    Условия намеренно узкие:

    - пунктов не меньше двух (один пункт по сетке раскладывать нечего);
    - у раскладки есть повтор и он развернулся ровно под столько единиц,
      сколько пунктов — частичная раскладка оставила бы часть сетки пустой,
      а это ровно тот мусор, от которого уходим;
    - в каждой единице нашлось место под текст.

    Слоты, ушедшие под пункты, удаляются из `by_role`: иначе тот же слот
    достался бы ещё и другому блоку слайда."""
    items = [i for i in block.items if i.strip()]
    if len(items) < 2 or pattern.repeat is None:
        return None
    groups = expand_repeat(pattern, len(items), grid)
    if len(groups) != len(items):
        return None

    result: list[SlotContent] = []
    used: set[int] = set()
    for item, group in zip(items, groups):
        body_slot = _pick_body_slot(group)
        if body_slot is None:
            return None  # единица повтора без места под текст — приём не годится
        result.append(SlotContent(body_slot, "card_body", [Paragraph(item)]))
        used.add(id(body_slot))

    for slots in by_role.values():
        slots[:] = [s for s in slots if id(s) not in used]
    return result


def _assign_cards(
    block: CardBlock, pattern: Pattern, grid: Grid, drops: list[DroppedContent],
) -> list[SlotContent]:
    if not block.items:
        return []
    groups = expand_repeat(pattern, len(block.items), grid) if pattern.repeat is not None else []
    if not groups:
        # У раскладки нет повтора вовсе (или его слоты не нашлись) — класть
        # карточки некуда ни одну.
        _drop(drops, "cards", *(f"{c.title}: {c.body}" if c.title else c.body for c in block.items))
        return []
    result: list[SlotContent] = []
    lost_titles: list[str] = []
    lost_bodies: list[str] = []
    for card, group in zip(block.items, groups):
        body_slot = _pick_body_slot(group)
        title_placed = False
        for slot in group:
            if slot.role == "card_title":
                if card.title:
                    result.append(SlotContent(slot, "card_title", [Paragraph(card.title)]))
                title_placed = True
            elif slot is body_slot:
                result.append(SlotContent(slot, "card_body", [Paragraph(card.body)]))
        if card.title and not title_placed:
            lost_titles.append(card.title)
        if body_slot is None:
            lost_bodies.append(card.body)
    # Карточек больше, чем единиц повтора смогла дать `expand_repeat`, —
    # хвост не лёг никуда (сегодня `expand_repeat` всегда отдаёт ровно `n`
    # групп, но на его молчаливое обещание опираться не стоит).
    for card in block.items[len(groups):]:
        lost_bodies.append(f"{card.title}: {card.body}" if card.title else card.body)
    _drop(drops, "card_title", *lost_titles)
    _drop(drops, "card_body", *lost_bodies)
    return result


# Пробовал 25 сентября 2026 класть заголовок карточки в слот `kpi_value`,
# когда `card_title` в единице повтора нет: на VK Education это четыре
# синих кружка с номерами, и без этого заголовки карточек («71 заявка»)
# отбрасывались, а кружки оставались пустыми. Откачено — слот рассчитан на
# одну-две цифры, и «71 заявка» ломалось переносом на «71 / заяв / ка».
# Такой слот годится под заголовок карточки только если тот в него влезает,
# а решать это должен замер, а не роль.


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


def _assign_kpis(
    block: KpiBlock, by_role: dict[str, list[PatternSlot]], drops: list[DroppedContent],
) -> list[SlotContent]:
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

    # Task 13 продолжение, дефект обязательной проверки (контрольный
    # шаблон, слайд 6: "крупные цифры разбросаны без подписей"). Раньше `n`
    # считался ТОЛЬКО по числу `kpi_value`-слотов (`n = min(len(items),
    # len(value_slots))`), а подпись метрики `i` подставлялась, только если
    # `i < len(label_slots)` — на раскладке с 4 `kpi_value`, но только 2
    # `kpi_label` (реальный случай контрольного шаблона) третья и четвёртая
    # метрика получали крупное число В СЛОТ, а их подпись просто МОЛЧА
    # терялась — значение и подпись ОДНОЙ метрики расходились по разным
    # судьбам: одна половина на слайде, другая нигде. Пара ЗНАЧЕНИЕ+ПОДПИСЬ
    # теперь либо целиком уходит в свои kpi_value/kpi_label слоты, либо
    # (когда не хватает подписи или самого value-слота) целиком уходит в
    # текстовый фолбэк ниже — они никогда не разъезжаются по разным путям.
    result: list[SlotContent] = []
    fallback_items: list = []
    vi = li = 0
    for item in block.items:
        if vi >= len(value_slots):
            fallback_items.append(item)
            continue
        needs_label = bool(item.label)
        if needs_label and li >= len(label_slots):
            fallback_items.append(item)
            continue
        result.append(SlotContent(value_slots[vi], "kpi_value", [Paragraph(item.value)]))
        vi += 1
        if needs_label:
            result.append(SlotContent(label_slots[li], "kpi_label", [Paragraph(item.label)]))
            li += 1
    del value_slots[:vi]
    del label_slots[:li]

    # Task 13, находка обязательной проверки (живой прогон на queue-latency,
    # VK Tech): раскладку `kind="kpi"` шаблон может не нести вовсе (VK Tech
    # — 0 раскладок этого kind из 20), а `plan.variants.apply_variant`
    # обязан всё равно выбрать КАКУЮ-ТО раскладку (`kind_pool` деградирует
    # на любой доступный `kind` варианта, а не оставляет слайд несобранным)
    # — не-kpi раскладка не несёт `kpi_value`/`kpi_label` вовсе, и `vi=li=0`
    # оставляло KpiBlock ПОЛНОСТЬЮ потерянным (слайд с одним заголовком без
    # единой цифры — находка: пустые карточки на живом рендере, ни один
    # факт не попал на слайд). Запасной вариант — тот же приём, что уже
    # применяют `_assign_block` для BulletBlock ("card_body" как запасная
    # роль) и `_assign_quote` для QuoteBlock без роли "quote": метрики, не
    # уместившиеся (или не уместившиеся вовсе, ИЛИ не нашедшие пары) в
    # kpi-слоты, рендерятся строкой "значение — подпись" в обычном текстовом
    # слоте — хуже выделенного KPI-блока визуально, но не теряет ни один
    # факт молча и не разрывает пару значение/подпись.
    if fallback_items:
        fallback_slot = _take_one(by_role, "bullet") or _take_one(by_role, "body") or _take_one(by_role, "card_body")
        lines = [f"{item.value} — {item.label}" if item.label else item.value for item in fallback_items]
        if fallback_slot is not None:
            result.append(SlotContent(fallback_slot, "bullets", [Paragraph(line, bullet=True) for line in lines]))
        else:
            # Ни kpi-слотов, ни текстового слота под запасной вариант —
            # метрики (цифры, ради которых слайд и делался) не попадут на
            # слайд вовсе.
            _drop(drops, "kpi", *lines)

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


def _repeat_unit_coords(pattern: Pattern) -> list[float]:
    """Координаты единиц повтора вдоль его оси (по возрастанию) — по
    ТЕКСТОВЫМ слотам повтора, тот же порядок, в котором `DecorShape.
    repeat_index` нумерует декор той же группы."""
    repeat = pattern.repeat
    if repeat is None:
        return []
    members = [s for s in pattern.slots if s.role in repeat.slot_roles]
    coord = (lambda b: b.left) if repeat.axis == "x" else (lambda b: b.top)
    return sorted({round(coord(s.box), 3) for s in members})


def filled_repeat_units(pattern: Pattern, contents: list[SlotContent]) -> set[int]:
    """Номера единиц повтора (нумерация `_repeat_unit_coords`, она же
    `DecorShape.repeat_index`), в слоты которых на ЭТОМ слайде реально лёг
    непустой текст.

    Нужна `expand_decor` для случая `n is None` — на слайде нет
    `CardBlock`, разворачивать повтор не под что, но слоты повтора при
    этом НЕ зарезервированы (см. `_slots_by_role`) и вполне могут достаться
    обычному текстовому блоку: плашка под таким слотом заполнена и обязана
    остаться, а плашка под слотом, которому ничего не досталось, — пустой
    белый прямоугольник на пол-слайда."""
    repeat = pattern.repeat
    if repeat is None:
        return set()
    coords = _repeat_unit_coords(pattern)
    index_of = {c: i for i, c in enumerate(coords)}
    coord = (lambda b: b.left) if repeat.axis == "x" else (lambda b: b.top)
    filled: set[int] = set()
    for content in contents:
        if content.slot.role not in repeat.slot_roles:
            continue
        if not any(p.text.strip() for p in content.paragraphs):
            continue
        index = index_of.get(round(coord(content.slot.box), 3))
        if index is not None:
            filled.add(index)
    return filled


def expand_decor(
    pattern: Pattern, n: int | None, grid: Grid, filled: set[int] | None = None,
) -> list[DecorShape]:
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
    единственный вызывающий `expand_repeat`, см. `_assign_cards`): двигать
    и размножать плашки не под что, их РОДНЫЕ позиции остаются как
    намайнены — но рисуются только те, под чьим слотом реально есть текст
    (`filled`, см. `filled_repeat_units`). Слоты повтора в этом случае не
    зарезервированы (см. `_slots_by_role`) и могут достаться обычному
    текстовому блоку, поэтому "что легло" считается по факту укладки, а не
    по наличию `CardBlock`. Находка ручной проверки (контрольный шаблон,
    слайд "Итоги 2026"): раскладка на три карточки досталась слайду с
    одним заголовком — три белые плашки 4×4 дюйма заняли больше половины
    слайда и не несли ни буквы.

    `filled=None` — вызывающий не считал, что куда легло: трогать декор
    не на каком основании, переносится весь, как намайнен (прежнее
    поведение)."""
    grouped = [d for d in pattern.decor if d.repeat_group]
    ungrouped = [d for d in pattern.decor if not d.repeat_group]
    if not grouped or pattern.repeat is None:
        return list(pattern.decor)
    if n is None:
        return _decor_of_filled_units(pattern, grouped, ungrouped, filled)
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
            copy = replace(d, box=with_axis(d.box, pos), repeat_index=i)
            # Значок-НОМЕР перенумеровывается по позиции в сетке: эталон
            # взят с первой единицы повтора, и без этого все четыре кружка
            # шаблона выходили с цифрой «1» (живой рендер 25 сентября 2026,
            # VK Education). Значок-буква или единица измерения не трогается
            # — она одинакова во всех единицах по замыслу.
            if copy.badge_text and copy.badge_text.strip().rstrip(".").isdigit():
                suffix = "." if copy.badge_text.strip().endswith(".") else ""
                copy = replace(copy, badge_text=f"{i + 1}{suffix}")
            result.append(copy)
    return result


def _decor_of_filled_units(
    pattern: Pattern, grouped: list[DecorShape], ungrouped: list[DecorShape], filled: set[int] | None,
) -> list[DecorShape]:
    """Декор группы повтора на РОДНЫХ позициях, но только тех единиц, в
    слоты которых что-то легло (`filled`).

    Единицы декора и единицы текстовых слотов сопоставляются ПО НОМЕРУ (и
    те и другие пронумерованы по возрастанию координаты вдоль оси повтора)
    — но только когда их одинаковое количество. Когда количества разошлись
    (декор мог сгруппироваться иначе, чем текстовые слоты), номера ничего
    не значат и выдумывать соответствие нельзя: решаем по группе целиком —
    легло хоть во что-то, оставляем весь декор, не легло никуда, не рисуем
    ни одной плашки.

    Порядок уцелевшего декора — ИСХОДНЫЙ (фильтр по `pattern.decor`, а не
    склейка "сначала негрупповой, потом групповой"): порядок декора — это
    порядок отрисовки, то есть кто под кем лежит."""
    if filled is None:
        return list(pattern.decor)
    unit_count = len({d.repeat_index for d in grouped})
    if unit_count != len(_repeat_unit_coords(pattern)):
        return list(pattern.decor) if filled else ungrouped
    # Пробовал 25 сентября 2026 оставлять КАРТИНКИ незаполненных единиц
    # повтора — рассуждая, что иконка без подписи всё равно выглядит
    # оформлением. Живой рендер показал обратное: семь синих квадратов
    # вразброс, текст под двумя из них. Осиротевшая иконка читается не как
    # украшение, а как потерянная карточка, ровно так же, как пустая
    # плашка. Правило прежнее: единица повтора рисуется целиком или не
    # рисуется вовсе.
    return [d for d in pattern.decor if not d.repeat_group or d.repeat_index in filled]


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
