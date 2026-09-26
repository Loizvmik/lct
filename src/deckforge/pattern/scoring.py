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
from deckforge.pattern.forms import MIN_HEADLINE_CHARS, PatternForm, list_item_limit
from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.style import StylePolicy

# Героические виды: картинка в них часть оформления, пустой её не считаем.
_HERO_IMAGE_KINDS = frozenset({"section", "image", "closing"})
# Плотность примера неизвестна (тестовые фикстуры, старый кэш): средний
# штраф, чтобы такие раскладки не выигрывали у измеренных даром.
_UNKNOWN_DENSITY_GAP = 0.3


def overflow(intent: SlideIntent, form: PatternForm, style: StylePolicy) -> float:
    """Доля содержания, которой раскладка не вмещает: лишние единицы плюс
    нехватка слов на единицу против нижней границы стиля. Ноль, если
    влезает. Оценка по вместимости, а не замер: текста ещё нет, его
    напишут под контракт, и задача здесь не пустить стиль туда, где даже
    короткий текст не поместится."""
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
    per_unit = list_item_limit(main, count).max_words
    floor = style.min_words_per_item
    word_gap = max(0.0, (floor - per_unit) / floor) if per_unit and floor else 0.0
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
        return form.has_chart or form.has_table
    if token == "diagram":
        return form.repeated and form.decor >= max(form.units, 1)
    if token == "image":
        return form.kind == "image" or form.has_image
    if token in ("cards", "kpi", "quote", "bullets"):
        return form.kind == token or block == token
    return form.kind == token


def style_mismatch(intent: SlideIntent, form: PatternForm, style: StylePolicy) -> float:
    """0 для первого предпочтения стиля, до 0,5 для последнего, 1 вне
    списка. Героические слайды стиль не оценивает: у них одна форма."""
    if intent.is_hero or not style.prefer:
        return 0.0
    for i, token in enumerate(style.prefer):
        if matches(token, form):
            return 0.5 * i / len(style.prefer)
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
    if position == 0 and cover_id is not None and pattern.pattern_id != cover_id:
        cost += w("cover_miss")
    if (
        position == last and closing_ids and intent.outline_kind == "closing"
        and pattern.pattern_id not in closing_ids
    ):
        cost += w("cover_miss")
    return cost


def repeat_cost(pattern_id: str, previous: str | None, uses: int, style: StylePolicy) -> float:
    """Повтор подряд и повтор вообще: второй растёт с числом уже сделанных
    повторов, а не включается один раз."""
    cost = style.weight("consecutive_repeat") if pattern_id == previous else 0.0
    return cost + style.weight("repeated_pattern") * uses
