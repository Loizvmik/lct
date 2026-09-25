"""Три варианта вёрстки ОДНОГО и того же содержания (Task 13 брифа) —
последний шаг планирования: `outline.build_outline` решает структуру,
`writer.write_slides` пишет текст, `writer.pick_patterns` (необязательно)
уточняет раскладку моделью, а `apply_variant` здесь детерминированно (БЕЗ
модели — в отличие от соседей по пакету, `Variant`/`apply_variant` не берут
`llm` в сигнатуре брифа) решает, КАКОЙ `kind` и КАКОЙ конкретный
`pattern_id` шаблона получит каждый слайд одного и того же `DeckSpec`,
по-разному для каждого варианта.

## Ось различий (обоснование выбора — ТЗ прямо требует его в документации)

Выбранная ось — ПЛОТНОСТЬ и ПОДБОР ПАТТЕРНОВ, не переписывание текста:
контент (числа, формулировки, источники) у всех трёх вариантов совпадает
дословно (см. `test_three_variants_carry_the_same_facts` — та же причина,
по которой `apply_variant` никогда не удаляет и не порождает текст, только
переставляет уже написанные блоки между слайдами и/или меняет `kind`,
которым эти блоки лягут на раскладку):

| Вариант | Плотность | Подбор паттернов | Группировка |
|---|---|---|---|
| `dense` | максимум фактов на слайд | предпочитает `cards`/`two_col`/`table`, паттерны победнее декором | без дополнительных разделителей |
| `airy` | один тезис на слайд | предпочитает `kpi`/`section`/`bullets` | разделитель перед каждым содержательным слайдом |
| `visual` | текст подчинён картинке | предпочитает `image`/`cards`, паттерны богаче декором | без дополнительных разделителей |

Почему НЕ меняется общее число исходных слайдов при слиянии/дроблении
контента: содержательная единица (буллет/карточка/KPI), однажды написанная
`writer.write_slides` под РЕАЛЬНУЮ вместимость раскладки её исходного
`kind`, не подгонит себя заново под ДРУГУЮ вместимость после слияния двух
слайдов в один без повторного вызова модели (`apply_variant` намеренно не
зовёт модель) — рисковать потерей/порчей факта ради более эффектной
разницы в числе слайдов значило бы нарушить куда более важное требование
ТЗ ("одинаково соответствуют правилам шаблона", проверяется детерминированным
аудитом на КАЖДОМ варианте). Дробление в одну сторону ВСЕ ЖЕ безопасно и
применяется: `airy` вставляет ЧИСТО ДОБАВОЧНЫЕ слайды-разделители (короткий
статичный заголовок без единой цифры, см. `_DIVIDER_LABELS`) перед каждым
содержательным слайдом — новый текст не изобретается из содержания (не
может исказить факты), только добавляет паузу ритма колоды, до потолка
`MAX_SLIDES` (Task 13 брифа, "объём 10-15 слайдов"). `dense` и `visual`
не меняют число слайдов вовсе — их различие целиком в `kind`/`pattern_id`
и, тем самым, в реальной раскладке шаблона.

## Почему `pattern_id`, а не только `kind`

`apply_variant` не может позволить себе оставить `pattern_id=None` и
переложить финальный выбор на `compose.builder._pick_pattern` целиком: три
варианта БЕЗ конкретного `pattern_id` дали бы одну и ту же раскладку
каждый раз, когда паттернов одного `kind` в шаблоне ровно один (частый
случай на реальных шаблонах датасета — не у каждого `kind` есть несколько
кандидатов) — "визуально различимы" тогда физически неоткуда взяться.
Поэтому `apply_variant` сам ранжирует паттернов ОДНОГО `kind` по вместимости
(`Capacity`, те же числа, что `writer._kind_capacity` использует для
лимитов текста) и, для `dense`/`visual`, по числу декоративных фигур
(`len(pattern.decor)` — ЧИСЛО, не сама геометрия: `plan/` по-прежнему не
читает ни одной координаты `Box`). `compose.builder.build_deck` не обязан
слепо доверять этому выбору — он перепроверяет `pattern_id` реальным
замером текста (`fits()`) и при плохом результате берёт лучший по
геометрии сам (см. докстроку `compose.builder._resolve_pattern`) — то же
"модель/код предлагает, сборка перепроверяет", что и у `pick_patterns`.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from enum import Enum

from deckforge.plan.spec import (
    BulletBlock, CardBlock, DeckSpec, KpiBlock, QuoteBlock, SLIDE_KINDS, SlideSpec, TextBlock,
)

# Объём колоды — то же ограничение, что `outline.MIN_SLIDES`/`MAX_SLIDES`
# (ТЗ дословно: "10-15 слайдов").
MIN_SLIDES = 10
MAX_SLIDES = 15


class Variant(Enum):
    """Единственное определение варианта вёрстки в проекте — `compose.
    builder` импортирует его отсюда (не заводит свою копию), т.к. `compose/`
    уже зависимо от `plan/` (`compose.blocks`/`compose.builder` импортируют
    `plan.spec` для типов содержания), а не наоборот — тот же однонаправленный
    порядок зависимостей, что и у остального проекта."""

    dense = "dense"
    airy = "airy"
    visual = "visual"


# Порядок предпочтения `kind` для каждого варианта (см. докстроку модуля,
# таблицу) — используется только СРЕДИ `kind`, СОВМЕСТИМЫХ со структурой
# уже написанного содержания слайда (`_compatible_kinds`): вариант выбирает
# ЛУЧШИЙ из того, что физически можно нарисовать без потери контента, а не
# любой ценой навязывает предпочтение.
# Task 18: три новых вида вплетены в предпочтение каждого варианта тем же
# принципом, что и остальные семь — по оси самого варианта (см. таблицу
# докстроки модуля), не произвольно приписаны в конец:
# `quote`/`kpi_caption` — героические, малословные формы ("один тезис на
# слайд"), ближе всего по духу к `kpi`/`section` — у `airy` они СРАЗУ после
# `kpi`, у `dense`/`visual` — заметно позже (плотность/декор важнее
# героики). `photo_text` — визуально ведомая форма (фото несёт часть
# смысла), у `visual` сразу после `image`, у `dense` (текст важнее
# картинки) — позже, у `airy` — там же, где `image` (просторная форма).
_VARIANT_KIND_PRIORITY: dict[Variant, tuple[str, ...]] = {
    Variant.dense: (
        "cards", "two_col", "table", "bullets", "kpi", "kpi_caption",
        "image", "photo_text", "section", "quote",
    ),
    Variant.airy: (
        "kpi", "kpi_caption", "quote", "section", "bullets", "two_col",
        "cards", "image", "photo_text", "table",
    ),
    Variant.visual: (
        "image", "photo_text", "cards", "kpi", "kpi_caption", "quote",
        "two_col", "bullets", "table", "section",
    ),
}

# Полными фразами, не голыми словами-темами ("Данные", "Риски") — находка
# координатора (обязательная проверка Task 13): AGENT.md outline-writer
# требует от МОДЕЛИ, чтобы заголовок был утверждением, а не темой
# ("Узкое место — не работа, а ожидание", не "Анализ процесса"); разделитель
# использует ТОТ ЖЕ героический `kind="section"`, что и настоящие
# содержательные слайды (визуально неотличим по начертанию/кеглю) — голое
# слово-тема в нём читается как брак того же рода, что и у содержательного
# слайда, даже когда голова слайда сгенерирована кодом, а не моделью.
_DIVIDER_LABELS = (
    "Дальше — контекст", "Дальше — цифры", "Дальше — решение",
    "Дальше — результаты", "Дальше — риски", "Дальше — план", "Дальше — итоги",
)


# ---------------------------------------------------------------------------
# Совместимость kind с уже написанным содержанием слайда
# ---------------------------------------------------------------------------


# Число элементов `KpiBlock`, ниже которого содержание — ОДИН героический
# фактоид (Task 18, вид `kpi_caption`), а не панель из нескольких метрик
# (`kind="kpi"`): 1 — буквально "одно число", не наблюдение за файлами, а
# сама граница смысла между "фактоид" и "панель" (панель начинается с двух).
_KPI_CAPTION_MAX_ITEMS = 1


def _kinds_that_hold_cards(profile) -> tuple[str, ...]:
    """Виды раскладок этого шаблона, среди которых есть хоть одна с
    повтором по ролям карточек. `cards` идёт первым, если он есть: при
    прочих равных родной вид всё-таки предпочтительнее."""
    if profile is None:
        return ()
    kinds = {
        p.kind for p in profile.patterns
        if p.repeat is not None and set(p.repeat.slot_roles) & {"card_title", "card_body"}
    }
    if not kinds:
        return ()
    ordered = ["cards"] if "cards" in kinds else []
    ordered += sorted(k for k in kinds if k != "cards")
    return tuple(ordered)


def _compatible_kinds(slide: SlideSpec, profile=None) -> tuple[str, ...]:
    """`kind`, на раскладку которых содержание `slide` ляжет БЕЗ ПОТЕРИ
    контента структурно (не по замеру — замер и ужимание делает `compose`
    при сборке, здесь только "физически есть куда положить эту РОЛЬ
    содержания"): `CardBlock` требует `Pattern.repeat` с ролями
    `card_title`/`card_body` (гарантированно только у `kind="cards"` —
    `compose.blocks._assign_cards` иначе молча ничего не разложит, см. её
    докстроку про `pattern.repeat is None -> []`), `KpiBlock` требует
    нескольких `kpi_value`/`kpi_label` слотов (гарантированно только у
    `kind="kpi"`, ОДИН элемент — у `kind="kpi_caption"` тоже, см. `_KPI_
    CAPTION_MAX_ITEMS`); `TableVisual` — `table`; безблочный слайд (только
    заголовок) — героический `kind="section"`. Остальное (текст/буллеты
    без карточек и KPI) достаточно гибко для нескольких `kind` —
    `compose.blocks` раскладывает их ПО РОЛИ слота, а не по `kind` пакета
    целиком, так что список этих "гибких" `kind` НАМЕРЕННО не включает
    `"section"` (геройские раскладки почти всегда несут только заголовок —
    вписать туда список пунктов было бы для содержания молча потерянным,
    см. докстроку модуля про то, почему `apply_variant` вообще не рискует
    содержанием).

    Task 18, находка №2 брифа ("модель не знает, какие формы умеет шаблон"):
    раньше `QuoteBlock` не проверялся здесь ВООБЩЕ — даже слайд, который
    slide-writer честно написал с `kind="quote"` и `QuoteBlock`, "теряло" эту
    форму на этом шаге (единственном месте, где `kind` окончательно
    проставляется на сборку, см. докстроку модуля — `write_slides`/
    `pick_patterns` их не вызывают в реальном пайплайне вовсе, `cli.py`/
    `api/jobs.py` идут прямо от `write_slides` к `apply_variant`): `has_quote`
    отсутствовал в проверках выше, слайд с одним `QuoteBlock` (не
    `TextBlock`/`BulletBlock`) проваливался в `n_text=0, has_bullets=False`
    и молча получал `kind="bullets"` по умолчанию — весь смысл написанной
    моделью цитаты для ПОДБОРА раскладки терялся здесь, даже когда в
    шаблоне есть настоящая раскладка-цитата. `compose.blocks._assign_quote`
    (уже умеет класть `QuoteBlock` и в `"quote"`-слот, и, если его нет, в
    `"body"`/`"card_body"`) не единственная причина, почему это раньше
    "работало" — работало ХУЖЕ, чем могло: содержание не терялось, но
    ВИЗУАЛЬНО цитата ложилась как обычный абзац."""
    has_card = any(isinstance(b, CardBlock) and b.items for b in slide.blocks)
    kpi_block = next((b for b in slide.blocks if isinstance(b, KpiBlock) and b.items), None)
    has_table = slide.visual is not None and slide.visual.table is not None
    has_quote = any(isinstance(b, QuoteBlock) and b.text.strip() for b in slide.blocks)
    n_text = sum(1 for b in slide.blocks if isinstance(b, TextBlock))
    has_bullets = any(isinstance(b, BulletBlock) and b.items for b in slide.blocks)

    if has_card:
        # Вид раскладки — ярлык, снятый ГЕОМЕТРИЕЙ, а держать карточки
        # умеет не только он: любая раскладка с повтором по ролям карточек
        # разложит их не хуже (`compose.blocks._assign_cards` смотрит на
        # `Pattern.repeat`, а не на `Pattern.kind`).
        #
        # Живой прогон 25 сентября 2026 на VK Education: все шесть
        # раскладок вида `cards` идут БЕЗ единого украшения, а из шести
        # двухколоночных украшены пять — и одна из них, `slide26`, несёт
        # ровно такой повтор. Жёсткая привязка к `cards` запирала слайд в
        # голых раскладках, хотя рядом была подходящая и оформленная.
        return _kinds_that_hold_cards(profile) or ("cards",)
    if kpi_block is not None:
        if len(kpi_block.items) <= _KPI_CAPTION_MAX_ITEMS:
            return ("kpi_caption", "kpi")
        return ("kpi",)
    if has_table:
        return ("table",)
    if has_quote:
        # `quote` первый по предпочтению, но НЕ единственный совместимый —
        # шаблон может не нести ни одной раскладки-цитаты вовсе (обычный
        # случай на бедных шаблонах), тогда `_assign_quote` всё равно
        # разложит текст цитаты в "body"/"card_body" ближайшего гибкого
        # `kind` (см. докстроку выше), не теряя содержание.
        return ("quote", "section", "bullets")
    if not slide.blocks and slide.visual is None:
        return ("section",)
    # Найдено этой задачей ("разбор незнакомого шаблона в бюджет", находка
    # №6, дефект рендера): слайд, который outline/slide-writer осознанно
    # написали `kind="section"` (герой-заголовок + ОДНА короткая мысль —
    # обычный стиль открывающего/переходного слайда, не список), раньше сюда
    # не доходил вовсе — "section" был совместим ТОЛЬКО с полностью
    # безблочным слайдом (проверка `not slide.blocks` строкой выше), а
    # единственный `TextBlock` сразу проваливался в ветку ниже
    # (`n_text >= 1 -> ("bullets", "two_col")"), теряя авторский выбор
    # `kind` целиком. На богатом шаблоне (VK Education) это выбирало
    # раскладку `two_col` — ДВЕ колонки контента под ОДИН текстовый блок:
    # первая колонка получала контент, вторая (`bullet`-слот справа) —
    # ничего, и на слайде оставался пустой декор второй колонки (в этом
    # конкретном шаблоне — сплошная чёрная плашка-подложка без текста,
    # живой дефект обязательной проверки этой задачи). "section" — ровно та
    # раскладка, слот которой (`body`/`caption`) рассчитан на одну короткую
    # мысль без второй колонки; `bullets` остаётся запасным вариантом на
    # случай, если у шаблона вовсе нет `section`-раскладки с подходящей
    # вместимостью (`two_col` НЕ в пуле — её вторая колонка не про этот
    # случай, см. выше, а не смежный вариант вкуса).
    if slide.kind == "section" and not has_bullets and n_text <= 1 and slide.visual is None:
        return ("section", "bullets")
    if slide.visual is not None and slide.visual.kind in ("photo", "icon"):
        n_text_with_photo = sum(1 for b in slide.blocks if isinstance(b, (TextBlock, BulletBlock)) and _block_has_text(b))
        if n_text_with_photo >= 1:
            # Фото/мокап РЯДОМ с содержательным текстом — Task 18,
            # `photo_text` (не героическая картинка на весь холст без
            # текста, для неё `image` остаётся первым в списке ниже, когда
            # текста на слайде фактически нет).
            return ("photo_text", "image", "bullets", "two_col")
        return ("image", "bullets", "two_col")

    if n_text >= 2:
        return ("two_col", "bullets")
    if has_bullets or n_text >= 1:
        return ("bullets", "two_col")
    return ("bullets",)


def _block_has_text(block) -> bool:
    if isinstance(block, TextBlock):
        return bool(block.text.strip())
    if isinstance(block, BulletBlock):
        return any(item.strip() for item in block.items)
    return False


def _content_char_len(slide: SlideSpec) -> int:
    """Длина самого длинного текстового элемента содержания слайда — та же
    единица, что `Capacity.max_chars_per_item` (`template/patterns.py`), но
    по ЕДИНИЦАМ РАЗМЕЩЕНИЯ, не по литеральным пунктам блока: `CardBlock`
    кладёт КАЖДУЮ карточку в СВОЙ слот (`compose.blocks._assign_cards`,
    `expand_repeat`) — единица содержания там и есть один пункт. `BulletBlock`
    — наоборот, кладёт ВСЕ пункты СПИСКОМ В ОДИН слот (`_assign_block`,
    `_take_one(by_role, "bullet")` — единственный слот на весь блок, без
    распределения по числу пунктов) — единица содержания там ВЕСЬ список
    целиком, а не самый длинный отдельный пункт.

    Находка обязательной проверки Task 13 (контрольный шаблон ЛЦТ2026,
    вмешательство координатора после первого прогона): раскладка `slide18`
    (`two_col`, `repeat=None`, `max_chars_per_item=117` — от САМОГО ШИРОКОГО
    из full19 мелких "body"-слотов сетки) казалась вместительной по замеру
    "самого длинного пункта" (4 факта по 50-62 знака каждый, максимум 62 <
    117 — раскладка выглядела подходящей), а на деле все 4 пункта ушли ОДНИМ
    блоком (224 знака) в ОДИН слот — переполнение, слишком мелкий текст,
    "нижние две трети пусты" (пункты не распределились по 19 слотам сетки,
    туда способен только `CardBlock`). Известный долг брифа ("Планирование
    обязано учитывать, что шаблон умеет") оказывается верным и здесь
    по-новому: раскладка с тем же числом карточек/пунктов, но втрое меньшей
    ФАКТИЧЕСКИ используемой текстовой вместимостью, даёт видимое
    переполнение — замер обязан отражать РЕАЛЬНОЕ распределение по слотам,
    не количество пунктов исходного блока."""
    lengths = [len(slide.headline or ""), len(slide.subhead or "")]
    for block in slide.blocks:
        if isinstance(block, TextBlock):
            lengths.append(len(block.text))
        elif isinstance(block, BulletBlock):
            # Один слот на весь список (см. докстроку) — сумма длин пунктов
            # (плюс перенос строки между ними, тот же порядок, что и
            # реальный текст, который уйдёт в textfit.measure при сборке).
            lengths.append(sum(len(item) + 1 for item in block.items))
        elif isinstance(block, CardBlock):
            lengths.extend(len(card.body) for card in block.items)
        elif isinstance(block, KpiBlock):
            lengths.extend(len(kpi.value) + len(kpi.label) for kpi in block.items)
    return max(lengths, default=0)


def _item_count(slide: SlideSpec) -> int | None:
    """Фактическое число ЕДИНИЦ РАЗМЕЩЕНИЯ слайда — сравнивается с
    `Capacity.max_items`, который сам считается от `pattern.repeat`
    (`template/patterns.py::_capacity`: без репита — заглушка `1`, "один
    слот", не "один пункт"). `CardBlock`/`KpiBlock` кладут КАЖДЫЙ пункт в
    СВОЙ слот через `expand_repeat` — сравнение "число пунктов vs.
    `max_items`" для них содержательно.

    `BulletBlock` (Task 13, находка обязательной проверки — контрольный
    шаблон, `slide18`) — НАМЕРЕННО не считается здесь вовсе (возвращает
    `None`, `cap_bucket` в `_pattern_rank_key` тогда 0, не участвует в
    ранжировании): весь список уходит ОДНИМ блоком в ОДИН слот
    (`compose.blocks._assign_block`, ни один текущий код-путь не
    распределяет пункты `BulletBlock` по нескольким слотам), поэтому
    "4 пункта против `max_items=1`" читалось бы как жёсткое переполнение
    даже для нормальной вместительной раскладки с одним просторным слотом
    под многострочный список (VK Tech, `max_chars_per_item=476` — на такую
    раскладку 4 коротких пункта ложатся свободно). Единственная величина,
    которая ДЕЙСТВИТЕЛЬНО отражает вместимость BulletBlock-слота, —
    суммарная длина всех пунктов, уже посчитанная `_content_char_len`."""
    for block in slide.blocks:
        if isinstance(block, CardBlock) and block.items:
            return len(block.items)
        if isinstance(block, KpiBlock) and block.items:
            return len(block.items)
    if slide.visual is not None and slide.visual.table is not None and slide.visual.table.rows:
        return len(slide.visual.table.rows) - 1
    return None


@dataclass(frozen=True)
class _SelectionHistory:
    """Раскладки, уже выбранные для ПРЕДЫДУЩИХ слайдов ЭТОЙ же колоды, В
    ЭТОМ ЖЕ варианте — Task 18, находка №3 брифа: реальный выбор
    `pattern_id` для собранной колоды делает ЭТОТ модуль (`apply_variant`,
    не `compose.builder._pick_pattern` — тот выбирает заново только когда
    `apply_variant` не проставил `pattern_id` вовсе или аудит внутри
    `compose.builder._place_best_candidate` отклонил его выбор, см. её
    докстроку), поэтому диверсификация обязана жить здесь, а не (только) в
    `compose/builder.py` — штраф там же, но ниже по конвейеру, ничего не
    меняет, если `apply_variant` уже проставил один и тот же `pattern_id`
    каждому слайду вида `kind` заранее (см. `_resolve_pattern` в
    `builder.py`: явный `pattern_id` переставлен первым БЕЗУСЛОВНО)."""

    counts: dict[str, int] = field(default_factory=dict)
    last_pattern_id: str | None = None

    def with_choice(self, pattern_id: str | None) -> "_SelectionHistory":
        if pattern_id is None:
            return self
        counts = dict(self.counts)
        counts[pattern_id] = counts.get(pattern_id, 0) + 1
        return _SelectionHistory(counts=counts, last_pattern_id=pattern_id)


_EMPTY_HISTORY = _SelectionHistory()

# Те же веса и то же обоснование, что у `compose.builder._REPEAT_PREV_
# PATTERN_PENALTY`/`_REPEAT_ANYWHERE_PENALTY_STEP` (см. их докстроки) — два
# места считают штраф за повтор идентично НАМЕРЕННО: одна и та же колода,
# наблюдаемая тем же человеком на защите, не должна получать РАЗНОЕ понятие
# "это уже повторяется" в зависимости от того, `apply_variant` выбрал
# `pattern_id` сразу или `compose.builder` довыбирал его сам.
_REPEAT_PREV_PATTERN_PENALTY = 1.0
_REPEAT_ANYWHERE_PENALTY_STEP = 0.5


def _diversity_penalty(pattern_id: str, history: _SelectionHistory) -> float:
    penalty = _REPEAT_PREV_PATTERN_PENALTY if pattern_id == history.last_pattern_id else 0.0
    penalty += _REPEAT_ANYWHERE_PENALTY_STEP * history.counts.get(pattern_id, 0)
    return penalty


def _has_image_slot(p) -> bool:
    return any(slot.role in ("image", "icon") for slot in p.slots)


def _pattern_rank_key(
    p, variant: Variant, item_count: int | None, char_len: int, kind_rank: int, avoid: frozenset[str],
    history: _SelectionHistory = _EMPTY_HISTORY, needs_image: bool = False,
) -> tuple:
    """Ключ сортировки одного паттерна-кандидата — ОБЩИЙ для всех `kind` из
    предпочтения варианта (см. `_choose_kind_and_pattern`), не только внутри
    одного `kind`: живой прогон обязательной проверки задачи вскрыл, что
    ранжир СТРОГО внутри уже выбранного по предпочтению `kind` — брак. Если
    у предпочтительного для варианта `kind` (скажем, `two_col` для `dense`)
    ВСЕ раскладки этого шаблона слишком малы для фактической длины текста
    (VK Tech: `two_col` — 16-27 знаков на элемент, а факт содержания —
    полноценное предложение), а `kind` пониже по предпочтению (`bullets`)
    вмещает контент нормально — правильный выбор ВТОРОЙ, даже если он не
    первый по вкусу варианта. Поэтому вместимость (`char_gap`/`cap_gap`)
    сравнивается ПЕРЕД предпочтением `kind` (`kind_rank`) — стиль не должен
    побеждать явное переполнение текста."""
    max_chars = p.capacity.max_chars_per_item or 0
    if char_len <= 0:
        char_gap = 0.0
    elif max_chars <= 0:
        char_gap = 1.0  # раскладка вовсе не заявляет текстовой вместимости — худший случай
    else:
        char_gap = max(0.0, (char_len - max_chars) / char_len)

    max_items = p.capacity.max_items or 1
    cap_gap = abs(max_items - item_count) / max_items if item_count is not None else 0.0

    # Вместимость — грубая площадная ОЦЕНКА (`template/patterns.py::
    # estimate_slot_chars`, честно объявлено её же докстрокой не замером
    # глифов), не точный замер текста (тот считает `compose.builder.fits`
    # при реальной сборке и, если оценка здесь всё же ошиблась, сам берёт
    # раскладку получше — см. `compose.builder._resolve_pattern`). Округлять
    # gap до грубых КОРЗИН, а не сравнивать вещественные доли напрямую —
    # намеренно: два кандидата, отличающиеся на несколько знаков вместимости
    # (шум самой оценки), не обязаны стабильно "побеждать" друг друга — это
    # решение принадлежит СТИЛЮ варианта (`kind_rank`/`decor_bias` ниже),
    # раз оценка вместимости у обоих практически одинакова. Различие внутри
    # одной корзины игнорируется полностью, различие в РАЗНЫЕ корзины — нет
    # (0.15 — заметно больше типичного шума оценки, но всё ещё меньше
    # "вдвое больше/меньше нужного").
    char_bucket = 0 if char_gap <= 0.15 else (1 if char_gap <= 0.5 else 2)
    cap_bucket = 0 if cap_gap <= 0.15 else (1 if cap_gap <= 0.5 else 2)
    # Худшая из двух корзин, не лексикографическая пара (char сначала, cap
    # потом) — находка обязательной проверки (контрольный шаблон, `slide18`):
    # раскладка с обманчиво большим `max_chars_per_item` (снятым с САМОГО
    # ШИРОКОГО из полутора десятков мелких слотов сетки, а не с
    # РЕАЛЬНО используемого) давала char_bucket=0 и лексикографически
    # побеждала ДАЖЕ ПРИ severe cap_bucket=2 — то, что должно было
    # дисквалифицировать раскладку, тонуло во втором месте сравнения кортежей
    # и никогда не смотрелось. `max()` не даёт хорошему числу по одной оси
    # маскировать плохое число по другой.
    fit_bucket = max(char_bucket, cap_bucket)

    # Различимость трёх вариантов (брифом: "визуально различимы") на
    # реальном шаблоне физически не может держаться на одной лишь
    # непрерывной шкале вкуса (декор/вместимость) — живой прогон
    # обязательной проверки задачи показал, что при небольшом числе
    # кандидатов одного `kind` (VK Tech: 2-4 на `kind`) монотонные функции
    # вкуса `dense`/`airy`/`visual` систематически СХЛОПЫВАЮТСЯ на одном и
    # том же победителе — не потому что вкусы совпадают, а потому что вкусу
    # просто не из чего выбирать по-разному. `avoid` — паттерны, которые
    # ДРУГИЕ уже проставленные варианты выбрали для ЭТОГО ЖЕ слайда (см.
    # `apply_variant`: `visual` избегает выбора `dense`, `airy` избегает
    # выбора И `dense`, И `visual`) — предпочтение штрафуется ПОСЛЕ проверки
    # вместимости (`char_bucket`/`cap_bucket` впереди — различимость не
    # должна покупаться ценой видимого переполнения), но ПЕРЕД вкусом
    # (`kind_rank`/`decor_bias` ниже) — раз вместимость у кандидатов
    # сопоставима, лучше явно РАЗНАЯ раскладка, чем формально "любимая по
    # вкусу", но неотличимая от соседнего варианта.
    avoid_penalty = 1 if p.pattern_id in avoid else 0

    # Штраф за повтор ВНУТРИ уже собираемой последовательности слайдов
    # этого варианта (Task 18) — СРАЗУ после `avoid_penalty` (кросс-
    # вариантная различимость) и ДО `kind_rank`/стиля: вместимость и кросс-
    # вариантная различимость важнее диверсификации внутри варианта, а
    # диверсификация внутри варианта важнее чистого вкуса `kind_rank`/декора
    # (тот же порядок приоритетов, что обосновывает позицию `avoid_penalty`
    # выше в докстроке функции — "различимость... ПЕРЕД вкусом").
    repeat_penalty = _diversity_penalty(p.pattern_id, history)

    # `visual` тянется к раскладкам богаче декором (декор часто И ЕСТЬ
    # картинка/пиктограмма). `dense` и `airy` декор не смещает вовсе.
    #
    # Раньше `dense` тянулся к раскладкам ПОБЕДНЕЕ декором — «плотнее
    # текстом, меньше оформления». Замер 25 сентября 2026 на VK Education
    # показал, во что это обходится: плотный вариант (он же первый, который
    # человек открывает) собирался из пяти самых голых макетов шаблона и
    # выглядел стопкой белых листов, хотя в шаблоне 55 нарисованных слайдов.
    #
    # Плотность — про количество содержания на слайде, а не про отсутствие
    # оформления. За количество отвечает `capacity_bias` ниже, он у `dense`
    # и так тянет к раскладкам большей вместимости; отсутствие декора
    # добавляло к этому только потерю фирменного вида.
    # `visual` тянется к раскладкам богаче декором (декор часто И ЕСТЬ
    # картинка/пиктограмма). `dense` и `airy` декор не смещает вовсе.
    #
    # Больше оформления — лучше, для ВСЕХ вариантов; `visual` тянется вдвое
    # сильнее, это его ось.
    #
    # Первая попытка (25 сентября 2026) сделала хуже: выбор садился на
    # раскладки с богатой повторяющейся сеткой, содержания на все её
    # единицы не хватало, и на слайде оставались осиротевшие иконки без
    # подписей. Тогда предпочтение убрали.
    #
    # Вернули после того, как список научился раскладываться ПО ЕДИНИЦАМ
    # ПОВТОРА (`compose.blocks._spread_bullets_over_repeat`): три пункта
    # теперь занимают три ячейки, и сетка заполняется вместо того, чтобы
    # осиротеть. Без той правки это предпочтение вредно, с ней — нужно:
    # из 34 раскладок VK Education 20 идут вообще без декора, и при
    # безразличии выбор садился именно на них.
    decor = len(p.decor)
    decor_bias = -decor * (2 if variant is Variant.visual else 1)

    # Второй тай-брейк стиля — вместимость самой раскладки (`Capacity.
    # max_items`), не только её декор: `dense` тянется к раскладкам с
    # БОЛЬШЕЙ вместимостью (плотнее упаковывает возможное), `airy` — к
    # МЕНЬШЕЙ (просторнее, "один тезис на слайд" читается и в самой форме
    # раскладки, не только в числе слайдов/разделителях), `visual` вместимость
    # не смещает — её ось уже декор. Нужен отдельно от `decor_bias`: на
    # содержании, где декор двух кандидатов совпадает (частый случай — см.
    # отчёт задачи, обязательная проверка), `dense`/`airy` иначе выбирали бы
    # одну и ту же раскладку, различаясь только заголовком/разделителями.
    if variant is Variant.dense:
        capacity_bias = -p.capacity.max_items
    elif variant is Variant.airy:
        capacity_bias = p.capacity.max_items
    else:
        capacity_bias = 0

    # Третья ось стиля — вместимость ПО СИМВОЛАМ на элемент (не по числу
    # элементов, это уже `capacity_bias` выше): `dense` тянется к раскладкам
    # с меньшим `max_chars_per_item` (компактная рамка под факт, тесно —
    # это и есть "плотность"), `airy` — с бОльшим (простор вокруг
    # единственного тезиса, тот же принцип "воздуха", что и у `decor_bias`
    # выше, только по буквам, не по декору), `visual` не смещает. Не то же
    # самое, что `char_bucket` (тот сравнивает вместимость с ФАКТИЧЕСКОЙ
    # длиной текста, отсекая заведомо неподходящие раскладки, а не
    # выражает стилевой вкус варианта).
    if variant is Variant.dense:
        roominess_bias = max_chars
    elif variant is Variant.airy:
        roominess_bias = -max_chars
    else:
        roominess_bias = 0

    # Слайду с фотографией — раскладка, где для неё есть место. Стоит
    # СРАЗУ после «влезает ли текст» и выше всего остального: замер 24
    # прогонов 23 сентября 2026 показал ноль вставленных фотографий из двух
    # на КАЖДОМ прогоне обоих шаблонов. `_compatible_kinds` возвращает для
    # фото список видов, но держит в нём `bullets`/`two_col` запасными (на
    # шаблонах, где раскладок с картинкой нет вовсе), и выбор спокойно
    # садился на них. Признак не жёсткий фильтр, а штраф: шаблон без единой
    # раскладки с картинкой по-прежнему собирается, а сборка честно пишет
    # находку «нет слота под фото/иконку».
    image_penalty = 0 if (not needs_image or _has_image_slot(p)) else 1
    return (
        fit_bucket, image_penalty, avoid_penalty, repeat_penalty, decor_bias, kind_rank,
        capacity_bias, roominess_bias, -p.score,
    )


# `kind`, чей `Pattern.repeat`/визуал требует контента, который приносит
# ТОЛЬКО соответствующий блок (`CardBlock`/`KpiBlock`/`TableVisual`/фото) —
# см. `_compatible_kinds`: без такого блока `compose.blocks._assign_cards`/
# `_assign_kpis`/`builder._place_table_visual`/`_place_visual` физически
# нечем заполнить повторяющиеся слоты (или единственный слот-картинку) этого
# `kind`, они останутся пустыми декоративными рамками (Task 13, дефект
# отчёта задачи №2, ручная проверка ЛЦТ2026: безблочный слайд деградировал
# на "cards", т.к. это первый по вкусу `kind` в списке предпочтения `dense`,
# — четыре пустые карточные плашки на слайде "Содержание_1"). Используется
# ТОЛЬКО в деградации `_choose_kind_and_pattern` ниже, когда совместимый
# `kind` не представлен в шаблоне — обычный (не деградирующий) путь и так
# никогда не выбирает эти `kind` без нужного блока (`_compatible_kinds` их
# просто не предлагает).
#
# `"kpi_caption"` (Task 18) — тот же риск, что и `"kpi"`: раскладка ждёт
# `kpi_value`/`kpi_label`, без `KpiBlock` слот пуст. `"photo_text"` — тот же
# риск, что у любой раскладки, чей смысл несёт картинка: без `slide.visual`
# слот-фото пуст (то же самое, чем в этом же списке уже была бы `"image"`,
# если бы её когда-либо приходилось деградировать — `"image"` сюда НЕ
# добавлена намеренно: `_compatible_kinds` уже не предлагает её без визуала
# структурно ни в одной ветке, добавлять её в hard-requirement было бы
# избыточно, но безопасно; `"quote"` НЕ добавлена — `compose.blocks.
# _assign_quote` уже умеет положить `QuoteBlock`, а при его отсутствии и
# любой другой текст, в `"body"`/`"card_body"`-слот раскладки `quote`
# (см. её докстроку) — деградация на `quote` без QuoteBlock не оставляет
# слот пустым, тот же класс "гибких" `kind`, что `bullets`/`two_col`.
_HARD_REQUIREMENT_KINDS = frozenset({"cards", "kpi", "kpi_caption", "table", "photo_text"})


def _choose_kind_and_pattern(
    slide: SlideSpec, profile, variant: Variant, *,
    avoid: frozenset[str] = frozenset(), history: _SelectionHistory = _EMPTY_HISTORY,
) -> tuple[str, str | None]:
    """`kind`+`pattern_id` для один слайд — брифом: "выбирает раскладку из
    списка, который ей дал разбор шаблона... если предложит несуществующую,
    код отвергает и берёт подходящую по вместимости" — здесь код не
    "предлагает" вовсе, решает сам, тем же принципом (список кандидатов —
    ТОЛЬКО то, что реально есть в `profile.patterns`, никогда не выдуманное).

    Кандидаты СОБИРАЮТСЯ по всем `kind`, совместимым с содержанием
    (`_compatible_kinds`), сразу — не перебираются по одному в порядке
    предпочтения варианта, останавливаясь на первом же существующем `kind`
    (см. докстроку `_pattern_rank_key` про находку живого прогона: первый по
    вкусу `kind` может физически не вмещать текст ни одной своей
    раскладкой). Ранжир один на всех кандидатов сразу: вместимость важнее
    стиля, предпочтение варианта — лишь тай-брейк (`kind_rank`, позиция
    `kind` в списке предпочтения) при сопоставимой вместимости.

    Деградация (тест брифа `test_variant_falls_back_when_template_lacks_a_
    pattern_kind`): если ни один СОВМЕСТИМЫЙ `kind` не представлен в этом
    шаблоне вовсе — берутся кандидаты `kind` из предпочтения варианта, но
    НИКОГДА `kind` из `_HARD_REQUIREMENT_KINDS` (`cards`/`kpi`/`table`),
    если содержание структурно не несёт нужного блока (Task 13, дефект
    отчёта задачи №2 — раньше эта деградация брала ЛЮБОЙ `kind` из вкуса
    варианта без разбора, и безблочный слайд на шаблоне без `kind="section"`
    садился на "cards" только потому, что она первая по вкусу `dense`,
    оставляя карточные слоты пустыми). Если и БЕЗОПАСНЫХ кандидатов нет
    (шаблон целиком состоит из `cards`/`kpi`/`table`) — берётся ЛЮБОЙ `kind`
    из предпочтения варианта (тот же путь, что и раньше: неидеальный выбор
    лучше никакого); если и таких нет — буквально любой `kind`, какой в
    шаблоне есть. `pattern_id=None` — только если у профиля вообще нет ни
    одного паттерна ни одного `kind` (пустой шаблон, честный крайний
    случай)."""
    # Плотный вариант наследует раскладку, выбранную писателем текста.
    #
    # `plan.writer` даёт модели каталог раскладок и черновую сборку
    # (`compose.slide_tools`), и она вправе вернуть `layout_id` — номер
    # раскладки, под которую писала текст. Раньше этот выбор выбрасывался
    # ЗДЕСЬ, для всех трёх вариантов: замер 23 сентября 2026 показал, что
    # раскладка, по которой модель правила текст, совпала с раскладкой
    # собранной презентации у 8 слайдов из 46. Текст правился под одну
    # раскладку, слайд уезжал в другую.
    #
    # Наследует ОДИН вариант, а не все три: три варианта обязаны быть
    # различимы (ТЗ), а выбор у писателя один. `dense` — потому что он же
    # идёт первым в предвычислении `avoid` и остальные два его избегают.
    #
    # Номер проверяется по каталогу профиля: выдуманный не годится (та же
    # сверка, что `writer._validate_chosen_layout`, но здесь она защищает
    # от профиля ДРУГОГО шаблона, а не от фантазии модели).
    if variant is Variant.dense and slide.pattern_id:
        authored = next((p for p in profile.patterns if p.pattern_id == slide.pattern_id), None)
        if authored is not None:
            return authored.kind, authored.pattern_id

    compatible = _compatible_kinds(slide, profile)
    priority = _VARIANT_KIND_PRIORITY[variant]
    available = {p.kind for p in profile.patterns}
    item_count = _item_count(slide)
    char_len = _content_char_len(slide)

    ordered = [k for k in priority if k in compatible] + [k for k in compatible if k not in priority]
    kind_pool = [k for k in ordered if k in available]
    if not kind_pool:
        safe_priority = [k for k in priority if k not in _HARD_REQUIREMENT_KINDS or k in compatible]
        kind_pool = [k for k in safe_priority if k in available]
    if not kind_pool:
        kind_pool = [k for k in priority if k in available]
    if not kind_pool:
        if not available:
            return slide.kind, None
        kind_pool = [next(iter(available))]

    kind_rank = {kind: i for i, kind in enumerate(kind_pool)}
    candidates = [p for p in profile.patterns if p.kind in kind_pool]

    needs_image = slide.visual is not None and slide.visual.kind in ("photo", "icon")
    best = min(
        candidates,
        key=lambda p: _pattern_rank_key(
            p, variant, item_count, char_len, kind_rank[p.kind], avoid, history, needs_image,
        ),
    )
    return best.kind, best.pattern_id


# ---------------------------------------------------------------------------
# Разделители airy — чисто добавочные слайды (см. докстроку модуля)
# ---------------------------------------------------------------------------


def _make_divider(index: int, label: str) -> SlideSpec:
    return SlideSpec(index=index, kind="section", headline=label)


def _with_dividers(slides: list[SlideSpec]) -> list[SlideSpec]:
    """Вставляет короткий слайд-разделитель перед содержательными слайдами
    (брифом `airy`: "разделитель перед каждым блоком"), кроме первого
    (титул уже открывает колоду) и кроме случая, когда сам слайд — уже
    героический `kind="section"` без блоков (разделитель перед
    разделителем не нужен).

    `budget` — сколько разделителей вообще можно вставить и остаться в
    пределах `MAX_SLIDES` (ТЗ "10-15 слайдов"), посчитан ДО цикла: ни один
    ИСХОДНЫЙ слайд (значит, ни один факт) при этом никогда не пропускается
    — `for slide in slides[1:]` дописывает КАЖДЫЙ исходный слайд
    безусловно, бюджет ограничивает только число ДОБАВОЧНЫХ разделителей,
    не число исходных слайдов, которые остаются в результате."""
    if not slides:
        return slides
    budget = max(0, MAX_SLIDES - len(slides))
    result = [slides[0]]
    label_i = 0
    for slide in slides[1:]:
        is_bare_section = slide.kind == "section" and not slide.blocks and slide.visual is None
        if budget > 0 and not is_bare_section:
            result.append(_make_divider(len(result), _DIVIDER_LABELS[label_i % len(_DIVIDER_LABELS)]))
            label_i += 1
            budget -= 1
        result.append(slide)
    return result


def _pattern_choices(
    slides: list[SlideSpec], profile, variant: Variant, *, avoid_by_slide: dict[int, frozenset[str]] | None = None,
) -> dict[int, str | None]:
    """`id(slide) -> pattern_id`, выбранный для `variant` по каждому слайду
    `slides` — вынесено отдельной функцией, чтобы `apply_variant` могло
    посчитать выбор ОДНОГО варианта (`dense`) и передать его как контекст
    `avoid` следующему (см. её докстроку).

    Своя, ЛОКАЛЬНАЯ история диверсификации (Task 18) — этот проход строит
    выбор ДЛЯ ОДНОГО варианта целиком (`dense`/`visual`, см. вызовы в
    `apply_variant`), последовательно по слайдам, тем же принципом "штраф
    за уже выбранное" (см. `_diversity_penalty`), что и финальный проход в
    `apply_variant` — предпросмотр выбора `dense` тоже не обязан повторять
    одну и ту же раскладку на каждом bullets-слайде, раз он сам становится
    контекстом `avoid` для `visual`/`airy`."""
    avoid_by_slide = avoid_by_slide or {}
    result: dict[int, str | None] = {}
    history = _EMPTY_HISTORY
    for slide in slides:
        avoid = avoid_by_slide.get(id(slide), frozenset())
        _kind, pattern_id = _choose_kind_and_pattern(slide, profile, variant, avoid=avoid, history=history)
        result[id(slide)] = pattern_id
        history = history.with_choice(pattern_id)
    return result


def apply_variant(deck_spec: DeckSpec, profile, variant: Variant) -> DeckSpec:
    """Единственная точка, где `DeckSpec`, написанный ОДИН РАЗ `writer.
    write_slides`, превращается в ТРИ различающихся набора `kind`+
    `pattern_id` — см. докстроку модуля про ось различий и про то, почему
    содержание (текст/числа) остаётся дословно тем же.

    Различимость вариантов на бедном вкусом кандидатов шаблоне (2-4
    паттерна на `kind` на VK Tech) опирается не только на вкус
    (`decor_bias`/`capacity_bias` в `_pattern_rank_key`), но и на явное
    ВЗАИМНОЕ ИЗБЕГАНИЕ: `dense` считается первым, без оглядки ни на кого
    (эталон "по вкусу без компромиссов"); `visual` для КАЖДОГО слайда
    избегает паттерна, который для ЭТОГО ЖЕ слайда выбрал `dense` (если есть
    сопоставимая по вместимости альтернатива, см. `avoid_penalty` в
    `_pattern_rank_key` — избегание НЕ покупается ценой видимого
    переполнения); `airy` избегает ОБОИХ. Порядок (dense -> visual -> airy)
    — не иерархия важности вариантов, а просто порядок вычисления, дающий
    каждому следующему варианту всё, что уже выбрано раньше.

    Внутри КАЖДОГО отдельного варианта (Task 18) конечный цикл ниже несёт
    свою `_SelectionHistory`, растущую по ходу перебора слайдов — тот же
    штраф за повтор, что теперь стоит и в `compose.builder._pattern_rank_
    key` (см. её докстроку про то, почему одного этого штрафа там
    недостаточно: `apply_variant` — точка, которая РЕАЛЬНО решает
    `pattern_id`, `builder.py` лишь перепроверяет и, если проставленный
    здесь `pattern_id` не прошёл аудит, довыбирает сам)."""
    base_slides = list(deck_spec.slides)
    slides = _with_dividers(base_slides) if variant is Variant.airy else base_slides

    dense_choice: dict[int, str | None] = {}
    if variant is not Variant.dense:
        dense_choice = _pattern_choices(slides, profile, Variant.dense)

    visual_choice: dict[int, str | None] = {}
    if variant is Variant.airy:
        avoid_for_visual = {
            sid: frozenset({pid}) if pid else frozenset() for sid, pid in dense_choice.items()
        }
        visual_choice = _pattern_choices(slides, profile, Variant.visual, avoid_by_slide=avoid_for_visual)

    avoid_by_slide: dict[int, frozenset[str]] = {}
    for slide in slides:
        avoid = {dense_choice.get(id(slide)), visual_choice.get(id(slide))} - {None}
        avoid_by_slide[id(slide)] = frozenset(avoid)

    new_slides: list[SlideSpec] = []
    history = _EMPTY_HISTORY
    for i, slide in enumerate(slides):
        avoid = avoid_by_slide.get(id(slide), frozenset())
        kind, pattern_id = _choose_kind_and_pattern(slide, profile, variant, avoid=avoid, history=history)
        new_slides.append(replace(slide, index=i, kind=kind, pattern_id=pattern_id))
        history = history.with_choice(pattern_id)

    return DeckSpec(
        title=deck_spec.title, language=deck_spec.language, slides=new_slides, meta=dict(deck_spec.meta),
    )
