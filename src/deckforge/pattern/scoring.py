"""Стоимость назначения паттерна слайду (раздел 7.2): чем меньше, тем лучше.

    cost = overflow * HUGE
         + consecutive_repeat * 100 + repeated_pattern * 30
         + style_mismatch * 20 + density_mismatch * 10
         - pattern_quality * 5 - decor_richness * decor_weight * 10
         (+ пустое место под фото, + обложка/финал не на своей раскладке)

Взвешенная сумма, а не лексикографический кортеж, как было в
`plan.variants._pattern_rank_key`: кортеж решал слайд за слайдом, и
повтор одной нарядной раскладки нечем было уравновесить, кроме порядка
полей. Здесь вся колода оптимизируется разом (`planner`), и у каждого
соображения своя цена. Веса в `config/styles.yaml`.

Стоимость делится на две части: статическую (зависит только от слайда и
паттерна, считается один раз) и за повтор (зависит от уже выбранного
соседями, считается в поиске)."""
from __future__ import annotations
import math

from deckforge.pattern.candidates import capacity_units
from deckforge.pattern.forms import (
    MAX_PHOTO_VOID, MIN_HEADLINE_CHARS, PatternForm, capabilities_of, list_item_limit,
)
from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.style import StylePolicy
from deckforge.template.patterns import CHART_TIER_FRAME

# Героические виды: картинка в них часть оформления, пустой её не считаем.
_HERO_IMAGE_KINDS = frozenset({"section", "image", "closing"})
# Плотность примера неизвестна (тестовые фикстуры, старый кэш): средний
# штраф, чтобы такие раскладки не выигрывали у измеренных даром.
_UNKNOWN_DENSITY_GAP = 0.3
# Доля слов стиля на единицу, которая обязана лечь кеглем примера (задача
# V5): меньше, и раскладка тесна стилю, даже если ужатым кеглем клон
# текст ещё примет.
NATIVE_SHARE = 0.6


def overflow(intent: SlideIntent, form: PatternForm, style: StylePolicy) -> float:
    """Доля содержания, которой раскладка не вмещает: лишние единицы плюс
    нехватка слов на единицу против нижней границы стиля. Ноль, если
    влезает. Оценка по вместимости, а не замер текста: текста ещё нет, его
    напишут под контракт, и задача здесь не пустить стиль туда, где даже
    короткий текст не поместится.

    Если место измерено замером клона (`Limit.native_words`, задача V5),
    слова берутся кеглем примера, без ужимания, а нижняя граница не ниже
    `NATIVE_SHARE` от слов стиля на единицу: раскладка, где при родном
    кегле ложится меньше, тесна, и узнать это планировщик должен сам, а
    не от клона, отклонившего текст по бюджету кегля."""
    if intent.is_hero:
        return 0.0
    needs = intent.items
    main = form.main
    if needs <= 0 or main is None:
        return 0.0
    units = capacity_units(form)
    unit_gap = max(0, needs - units) / needs
    if main.block == "kpi":
        return unit_gap
    count = min(needs, units) or 1
    limit = list_item_limit(main, count)
    floor = style.min_words_per_item
    if intent.outline_kind in style.label_kinds:
        # Подписи (задача D3): мерка места та, что клон примет ужатым
        # кеглем, а не родным: замер клона таймлайна дал родному кеглю
        # ноль слов, а два слова события клон принимает.
        floor = style.label_words
        per_unit = limit.max_words
        return unit_gap + (max(0.0, (floor - per_unit) / floor) if floor and per_unit else 0.0)
    if limit.native_words is not None:
        per_unit = limit.native_words
        floor = max(floor, NATIVE_SHARE * style.words_per_item)
    else:
        per_unit = limit.max_words
    word_gap = max(0.0, (floor - per_unit) / floor) if floor and (per_unit or limit.native_words is not None) else 0.0
    return unit_gap + word_gap


def short_headline(form: PatternForm) -> float:
    """0..1: насколько рамка заголовка примера короче заголовка-вывода.
    Такую раскладку можно взять, но писатель заголовок в неё не уложит."""
    limit = form.headline
    if limit is None or not limit.max_chars:
        return 0.0
    return max(0.0, (MIN_HEADLINE_CHARS - limit.max_chars) / MIN_HEADLINE_CHARS)


def matches(token: str, form: PatternForm) -> bool:
    """Совпадает ли раскладка с предпочтением стиля. Кроме видов шаблона
    есть составные: `sparse_cards` (карточки, не больше трёх), `chart`
    (есть место под график или таблицу), `diagram` (повтор с декором на
    каждую единицу: схема, а не голый список), `image` (место под картинку)."""
    main = form.main
    block = main.block if main is not None else None
    if token == "sparse_cards":
        return block == "cards" and form.units <= 3
    if token == "chart":
        return form.has_chart or form.has_table or is_chart_frame(form)
    if token == "diagram":
        return form.repeated and form.decor >= max(form.units, 1)
    if token == "timeline":
        # Длинный ряд с точкой или засечкой на каждую единицу: таймлайн
        # или Гант (VK Education, примеры 42 и 43), а не сетка карточек.
        # Вид `timeline` разбор теперь ставит сам (линия, стрелки, даты).
        return form.kind == "timeline" or (form.repeated and form.units >= 5 and form.decor >= form.units)
    if token == "image":
        return form.kind == "image" or form.has_image
    if token in ("cards", "kpi", "quote", "bullets"):
        return form.kind == token or block == token
    return form.kind == token


def style_mismatch(intent: SlideIntent, form: PatternForm, style: StylePolicy) -> float:
    """0 для первого предпочтения стиля, до 0,5 для последнего, 1 вне
    списка. Героические слайды стиль не оценивает: у них одна форма."""
    prefer = style.prefer_of(intent.outline_kind)
    if intent.is_hero or not prefer:
        return 0.0
    for i, token in enumerate(prefer):
        if matches(token, form):
            return 0.5 * i / len(prefer)
    return 1.0


def density_mismatch(pattern, style: StylePolicy) -> float:
    density = getattr(pattern, "source_density", None)
    if density is None:
        return _UNKNOWN_DENSITY_GAP
    return abs(style.target_density - density)


def decor_richness(form: PatternForm) -> float:
    """0..1. Корень, а не число фигур: раскладка с шестью украшениями
    заметно наряднее голой, а с двумястами не в тридцать раз наряднее
    шести (опыт 25 сентября 2026, см. историю `plan.variants`)."""
    return min(1.0, math.sqrt(form.decor) / 3.0)


def static_cost(
    intent: SlideIntent, pattern, form: PatternForm, style: StylePolicy, *,
    position: int, last: int, cover_id: str | None, closing_ids: frozenset[str],
) -> float:
    w = style.weight
    cost = w("overflow") * overflow(intent, form, style)
    cost += w("style_mismatch") * style_mismatch(intent, form, style)
    cost += w("density_mismatch") * density_mismatch(pattern, style)
    cost += w("short_headline") * short_headline(form)
    cost -= w("pattern_quality") * float(pattern.score)
    cost -= w("decor") * style.decor_weight * decor_richness(form)
    if (
        form.has_image and intent.photo is None and intent.required_visual != "chart"
        and pattern.kind not in _HERO_IMAGE_KINDS and not intent.is_hero
    ):
        cost += w("orphan_image")
    cost += photo_void_cost(pattern, intent, style)
    cost += photo_fit_cost(pattern, intent, style)
    if intent.required_visual == "chart":
        cost += w("chart_fit") * style.chart_weight * chart_fit(form)
    elif intent.required_visual == "code":
        cost += w("chart_fit") * code_fit(form)
    if position == 0 and cover_id is not None and pattern.pattern_id != cover_id:
        cost += w("cover_miss")
    if (
        position == last and closing_ids and intent.outline_kind == "closing"
        and pattern.pattern_id not in closing_ids
    ):
        cost += w("cover_miss")
    return cost


# Ступень пригодности раскладки под график без места под него вовсе:
# хуже любой из трёх (`patterns.CHART_TIER_*`), такую раскладку берёт
# только ослабленный фильтр кандидатов.
_NO_CHART_PLACE = 3


def is_chart_frame(form: PatternForm) -> bool:
    """Картинка примера сама график: для вкуса стиля это место под график,
    как и родной график."""
    return form.chart_tier == CHART_TIER_FRAME


def chart_fit(form: PatternForm) -> float:
    """0 у родного графика примера (подменяются только данные, стиль
    дизайнера остаётся), 1 у картинки-графика (наш график встаёт в её
    рамку), 2 у крупного текстового места, 3 без места. Задача V1."""
    return float(_NO_CHART_PLACE if form.chart_tier is None else form.chart_tier)


def code_fit(form: PatternForm) -> float:
    """Задача T2: 0 у слота роли `code` (плашка и моногарнитура дизайнера),
    1 у крупного текстового места (плашку рисует сборка), 2 без места.
    Вес тот же, что у места графика: оба про качество места под визуал."""
    return float(2 if form.code_tier is None else form.code_tier)


def repeat_cost(pattern_id: str, previous: str | None, uses: int, style: StylePolicy) -> float:
    """Повтор подряд и повтор вообще: второй растёт с числом уже сделанных
    повторов, а не включается один раз."""
    cost = style.weight("consecutive_repeat") if pattern_id == previous else 0.0
    return cost + style.weight("repeated_pattern") * uses


def growing_repeat_cost(look: str, previous: str | None, uses: int, style: StylePolicy) -> float:
    """Повтор одного облика раскладки в колоде: цена каждого следующего
    повтора вдвое выше предыдущего (30, 60, 120...), а не линейна, как у
    `repeat_cost`. Линейной цены не хватало: живой прогон dense 27 сентября
    2026 посадил на четыре кружка шесть слайдов из двенадцати, потому что
    предпочтение стиля к карточкам на каждом слайде перевешивало очередные
    30. С удвоением третий повтор стоит 120 и дороже любого несовпадения
    со стилем.

    Подряд один и тот же облик запрещён, пока есть альтернатива: вес
    `consecutive_repeat` в конфиге порядка переполнения. `look`: ключ
    облика (`look_key`), не `pattern_id`: два примера шаблона с одной
    геометрией читаются глазом как одна раскладка."""
    cost = style.weight("consecutive_repeat") if look == previous else 0.0
    if uses > 0:
        cost += style.weight("repeated_pattern") * (2 ** (uses - 1))
    return cost


def look_key(pattern) -> str:
    """Облик раскладки: вид, лейаут, роли мест, повтор и число декора. У
    VK Education `slide21` и `slide44` разные примеры с одной и той же
    геометрией (четыре кружка с подписями), и повтор по `pattern_id` их
    не различал бы с разными: колода выходила монотонной при «разных»
    раскладках."""
    repeat = getattr(pattern, "repeat", None)
    rep = (
        (getattr(repeat, "axis", None), repeat.count, tuple(sorted(repeat.slot_roles)))
        if repeat is not None else None
    )
    roles = tuple(sorted(s.role for s in pattern.slots))
    decor = getattr(pattern, "decor", None) or ()
    return repr((pattern.kind, getattr(pattern, "layout_id", None), roles, rep, len(decor)))


def photo_void(pattern, intent: SlideIntent) -> float:
    """Доля холста, которая опустеет, когда клон удалит фото примера
    (`forms.PatternCapabilities.photo_void`)."""
    return capabilities_of(pattern).photo_void(bool(intent.photo))


def photo_void_cost(pattern, intent: SlideIntent, style: StylePolicy) -> float:
    """Штраф за пустоту на месте фото примера, если жёсткое ограничение
    пришлось ослабить: выше любого несовпадения стиля."""
    void = photo_void(pattern, intent)
    return style.weight("sample_photo_void") if void > MAX_PHOTO_VOID else 0.0



# Во сколько раз пропорции фото и рамки могут расходиться без штрафа:
# кадрирование по центру срезает до трети стороны, лицо и экран остаются.
PHOTO_ASPECT_TOLERANCE = 1.5
# Рамка под фото меньше этой доли холста: место иконки, а не фото.
MIN_PHOTO_FRAME_AREA = 0.04


def photo_aspect_mismatch(photo_aspect: float, frame_aspect: float) -> float:
    """Во сколько раз пропорции расходятся: 1 совпадают, 2 одна сторона
    вдвое длиннее, чем у другой. Порядок аргументов неважен."""
    return max(photo_aspect / frame_aspect, frame_aspect / photo_aspect)


def photo_fit_cost(pattern, intent: SlideIntent, style: StylePolicy) -> float:
    """Штраф раскладке под фото пользователя за рамку, в которую оно не
    ляжет: пропорции расходятся больше `PHOTO_ASPECT_TOLERANCE` (широкий
    скриншот в рамке телефона VK Education, пример 30, обрезался до
    полоски) или рамка размером с иконку. Растёт с расхождением, но не
    выше двух весов: совсем без рамки фото пропадёт, это хуже.

    Рамки раскладок (`SlideIntent.photo_frames`: пропорции в пикселях
    холста и доля холста) меряет вызывающий код по профилю: планировщик
    координат не знает."""
    frames = intent.photo_frames or {}
    frame = frames.get(pattern.pattern_id)
    if not intent.photo or frame is None:
        return 0.0
    frame_aspect, frame_area = frame
    cost = style.weight("photo_aspect") if frame_area < MIN_PHOTO_FRAME_AREA else 0.0
    if intent.photo_aspect and frame_aspect:
        mismatch = photo_aspect_mismatch(intent.photo_aspect, frame_aspect)
        if mismatch > PHOTO_ASPECT_TOLERANCE:
            cost += style.weight("photo_aspect") * min(2.0, mismatch / PHOTO_ASPECT_TOLERANCE)
    return cost
