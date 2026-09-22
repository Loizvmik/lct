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
from dataclasses import replace
from enum import Enum

from deckforge.plan.spec import (
    BulletBlock, CardBlock, DeckSpec, KpiBlock, SLIDE_KINDS, SlideSpec, TextBlock,
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
_VARIANT_KIND_PRIORITY: dict[Variant, tuple[str, ...]] = {
    Variant.dense: ("cards", "two_col", "table", "bullets", "kpi", "image", "section"),
    Variant.airy: ("kpi", "section", "bullets", "two_col", "cards", "image", "table"),
    Variant.visual: ("image", "cards", "kpi", "two_col", "bullets", "table", "section"),
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


def _compatible_kinds(slide: SlideSpec) -> tuple[str, ...]:
    """`kind`, на раскладку которых содержание `slide` ляжет БЕЗ ПОТЕРИ
    контента структурно (не по замеру — замер и ужимание делает `compose`
    при сборке, здесь только "физически есть куда положить эту РОЛЬ
    содержания"): `CardBlock` требует `Pattern.repeat` с ролями
    `card_title`/`card_body` (гарантированно только у `kind="cards"` —
    `compose.blocks._assign_cards` иначе молча ничего не разложит, см. её
    докстроку про `pattern.repeat is None -> []`), `KpiBlock` требует
    нескольких `kpi_value`/`kpi_label` слотов (гарантированно только у
    `kind="kpi"`); `TableVisual` — `table`; безблочный слайд (только
    заголовок) — героический `kind="section"`. Остальное (текст/буллеты/
    цитата без карточек и KPI) достаточно гибко для нескольких `kind` —
    `compose.blocks` раскладывает их ПО РОЛИ слота, а не по `kind` пакета
    целиком, так что список этих "гибких" `kind` НАМЕРЕННО не включает
    `"section"` (геройские раскладки почти всегда несут только заголовок —
    вписать туда список пунктов было бы для содержания молча потерянным,
    см. докстроку модуля про то, почему `apply_variant` вообще не рискует
    содержанием)."""
    has_card = any(isinstance(b, CardBlock) and b.items for b in slide.blocks)
    has_kpi = any(isinstance(b, KpiBlock) and b.items for b in slide.blocks)
    has_table = slide.visual is not None and slide.visual.table is not None

    if has_card:
        return ("cards",)
    if has_kpi:
        return ("kpi",)
    if has_table:
        return ("table",)
    if not slide.blocks and slide.visual is None:
        return ("section",)
    if slide.visual is not None and slide.visual.kind in ("photo", "icon"):
        return ("image", "bullets", "two_col")

    n_text = sum(1 for b in slide.blocks if isinstance(b, TextBlock))
    has_bullets = any(isinstance(b, BulletBlock) and b.items for b in slide.blocks)
    if n_text >= 2:
        return ("two_col", "bullets")
    if has_bullets or n_text >= 1:
        return ("bullets", "two_col")
    return ("bullets",)


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


def _pattern_rank_key(
    p, variant: Variant, item_count: int | None, char_len: int, kind_rank: int, avoid: frozenset[str],
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

    # `dense` тянется к раскладкам победнее декором (плотнее текстом, меньше
    # оформления), `visual` — к раскладкам богаче декором (декор часто И
    # ЕСТЬ картинка/пиктограмма), `airy` декор не смещает вовсе (её ось —
    # `capacity_bias`/`roominess_bias` ниже и разделители, не декор).
    decor = len(p.decor)
    if variant is Variant.dense:
        decor_bias = decor
    elif variant is Variant.visual:
        decor_bias = -decor
    else:
        decor_bias = 0

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

    return (fit_bucket, avoid_penalty, kind_rank, decor_bias, capacity_bias, roominess_bias, -p.score)


# `kind`, чей `Pattern.repeat`/визуал требует контента, который приносит
# ТОЛЬКО соответствующий блок (`CardBlock`/`KpiBlock`/`TableVisual`) — см.
# `_compatible_kinds`: без такого блока `compose.blocks._assign_cards`/
# `_assign_kpis`/`builder._place_table_visual` физически нечем заполнить
# повторяющиеся слоты этого `kind`, они останутся пустыми декоративными
# рамками (Task 13, дефект отчёта задачи №2, ручная проверка ЛЦТ2026:
# безблочный слайд деградировал на "cards", т.к. это первый по вкусу `kind`
# в списке предпочтения `dense`, — четыре пустые карточные плашки на
# слайде "Содержание_1"). Используется ТОЛЬКО в деградации `_choose_kind_
# and_pattern` ниже, когда совместимый `kind` не представлен в шаблоне —
# обычный (не деградирующий) путь и так никогда не выбирает эти `kind` без
# нужного блока (`_compatible_kinds` их просто не предлагает).
_HARD_REQUIREMENT_KINDS = frozenset({"cards", "kpi", "table"})


def _choose_kind_and_pattern(
    slide: SlideSpec, profile, variant: Variant, *, avoid: frozenset[str] = frozenset(),
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
    compatible = _compatible_kinds(slide)
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

    best = min(
        candidates,
        key=lambda p: _pattern_rank_key(p, variant, item_count, char_len, kind_rank[p.kind], avoid),
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
    `avoid` следующему (см. её докстроку)."""
    avoid_by_slide = avoid_by_slide or {}
    result: dict[int, str | None] = {}
    for slide in slides:
        avoid = avoid_by_slide.get(id(slide), frozenset())
        _kind, pattern_id = _choose_kind_and_pattern(slide, profile, variant, avoid=avoid)
        result[id(slide)] = pattern_id
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
    каждому следующему варианту всё, что уже выбрано раньше."""
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
    for i, slide in enumerate(slides):
        avoid = avoid_by_slide.get(id(slide), frozenset())
        kind, pattern_id = _choose_kind_and_pattern(slide, profile, variant, avoid=avoid)
        new_slides.append(replace(slide, index=i, kind=kind, pattern_id=pattern_id))

    return DeckSpec(
        title=deck_spec.title, language=deck_spec.language, slides=new_slides, meta=dict(deck_spec.meta),
    )
