"""Структура колоды под стиль: что стиль делает с пунктами общей структуры
до выбора раскладок (задача D3).

Структура у пакета одна на три стиля (`api.jobs.OutlineShare`): входы
одинаковые, и платить за вызов модели трижды незачем. Но одна структура с
одними числами единиц давала планировщику одинаковые задачи, и три стиля
садились почти на одни и те же карточки (VK Education, 27 сентября 2026):
различие оставалось в словах и плотности, а ТЗ требует визуально
различимых вариантов. Поэтому каждый стиль переделывает намерения слайдов
сам, до планирования, без модели:

- dense: соседние простые пункты сливаются в один слайд (не ниже
  `MIN_SLIDES`), на слайде 4-6 единиц, числа источника, которые никто не
  взял, идут таблицей;
- airy: не больше трёх единиц на слайд, один тезис; разделители и так
  добавляет планировщик;
- visual: каждый числовой ряд источника становится графиком, не только
  обязательный, одно-два числа показателем, единиц на слайде не больше
  четырёх: текст подчинён картинке.

Числа для таблиц и графиков берутся только из источников (слой
`plan.data_types`): стиль меняет форму показа, а не содержание."""
from __future__ import annotations
from dataclasses import replace

from deckforge.pattern.candidates import candidates_for, cover_pattern_id, has_chart_place, is_closing_pattern
from deckforge.pattern.forms import forms_of
from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.planner import TIGHT_OVERFLOW
from deckforge.pattern.scoring import overflow
from deckforge.pattern.style import load_style
from deckforge.plan.series import stems

# Нижняя граница объёма по ТЗ (10-15 слайдов): слияние dense до неё и
# останавливается.
MIN_SLIDES = 10

# Пункты, которые можно слить с соседом: у них перечень мыслей, а не особая
# форма. Повестку, сравнение, данные и просьбу не трогаем: у повестки своя
# роль в начале, у сравнения две стороны, у данных число, у просьбы одна
# мысль.
_MERGEABLE = frozenset({"context", "problem", "solution", "how_it_works", "case", "risks", "team"})
# Пункты, где плотный стиль добирает единиц до `DENSE_MIN_ITEMS`.
_LISTY = _MERGEABLE | {"agenda", "roadmap"}

DENSE_MIN_ITEMS = 4
DENSE_MAX_ITEMS = 6
AIRY_MAX_ITEMS = 3
VISUAL_MAX_ITEMS = 4
# Сколько раскладок должно вмещать число единиц, чтобы dense его брал.
ROOM_LAYOUTS = 2


def _plain(intent: SlideIntent) -> bool:
    """Простой пункт: без особой формы, фото и данных. Только такие стиль
    вправе переделывать: форму, заказанную структурой, и фото, уже
    распределённое по слайдам, решали до него."""
    return (
        not intent.is_hero and intent.form is None and intent.photo is None
        and intent.visual_intent is None
    )


def _words(intent: SlideIntent) -> set[str]:
    return stems(" ".join([intent.intent, *intent.needs]))


def _data_words(vi) -> set[str]:
    data = getattr(vi, "data", None)
    return set(data.words) if data is not None else set()


def _unused(intents: list[SlideIntent], visuals) -> list:
    taken = {getattr(it.visual_intent, "data_ref", None) for it in intents if it.visual_intent is not None}
    return [vi for vi in visuals or () if vi.data_ref not in taken]


def _attach(intents: list[SlideIntent], vi, form: str, visual) -> bool:
    """Отдать набор данных самому близкому по словам простому пункту.
    Без общих слов отдаётся пункту «данные»: он о числах, а у числа
    источника другого места нет. Иначе набор никому не достаётся:
    таблица сроков на слайде про команду хуже её отсутствия."""
    words = _data_words(vi)
    scored = [
        (len(_words(it) & words), it.outline_kind == "data", -i)
        for i, it in enumerate(intents)
    ]
    best = None
    for i, it in enumerate(intents):
        if not _plain(it):
            continue
        overlap, is_data, _ = scored[i]
        if overlap == 0 and not is_data:
            continue
        if best is None or scored[i] > scored[best]:
            best = i
    if best is None:
        return False
    intents[best] = replace(intents[best], form=form, items=1, visual_intent=visual)
    return True


def _merge(a: SlideIntent, b: SlideIntent, max_items: int) -> SlideIntent:
    return replace(
        a,
        intent=f"{a.intent.rstrip('. ')}; {b.intent[:1].lower()}{b.intent[1:]}",
        needs=(*a.needs, *b.needs),
        items=min(max_items, a.items + b.items),
        covers=(*a.outline_indices, *b.outline_indices),
    )


def room(profile, style) -> int:
    """Сколько единиц простого пункта шаблон вмещает в стиле `style` без
    тесноты хотя бы двумя раскладками (`ROOM_LAYOUTS`): потолок для
    слияния и добора dense. Шесть единиц на шаблоне, где больше четырёх не
    лежит нигде, планировщику пришлось бы ослаблять ограничения, а клон
    отклонил бы текст (VK Tech, 27 сентября 2026). Одной раскладки мало:
    все плотные слайды сели бы на неё, а повтор запрещён. Кандидаты те же,
    что у планировщика (`candidates_for`), без ослабления. Без профиля
    потолок стиля."""
    if profile is None or not getattr(profile, "patterns", None):
        return DENSE_MAX_ITEMS
    policy = load_style(style)
    forms = forms_of(profile)
    cover_id = cover_pattern_id(profile)
    closing = frozenset(p.pattern_id for p in profile.patterns if is_closing_pattern(p, profile))
    for n in range(DENSE_MAX_ITEMS, 1, -1):
        probe = SlideIntent(index=1, outline_kind="context", intent="", items=n)
        found = candidates_for(
            probe, profile, forms, position=1, last=MIN_SLIDES, cover_id=cover_id, closing_ids=closing,
        )
        if found.relaxed:
            continue
        roomy = [pid for pid in found.pattern_ids if overflow(probe, forms[pid], policy) < TIGHT_OVERFLOW]
        if len(roomy) >= ROOM_LAYOUTS:
            return n
    return 1


def places(profile) -> set[str]:
    """Какие места под визуал есть в шаблоне: `table`, `chart`, `kpi`.
    Без профиля считаем, что есть все: решает планировщик. Форму, которой
    шаблон не вмещает, стиль не назначает: планировщик ослабил бы
    ограничения, и таблица легла бы в колонки (VK Tech без таблиц)."""
    if profile is None or not getattr(profile, "patterns", None):
        return {"table", "chart", "kpi"}
    out: set[str] = set()
    for form in forms_of(profile).values():
        if form.has_table:
            out.add("table")
        if has_chart_place(form):
            out.add("chart")
        if form.main is not None and form.main.block == "kpi":
            out.add("kpi")
    return out


def _dense(
    intents: list[SlideIntent], visuals, most: int = DENSE_MAX_ITEMS, allowed=frozenset({"table"}),
) -> list[SlideIntent]:
    result = list(intents)
    for vi in _unused(result, visuals) if "table" in allowed else ():
        if getattr(vi, "type", None) not in ("table", "chart"):
            continue
        # Плотный стиль показывает числа таблицей: в ней видно каждое
        # значение, и она держит больше, чем график.
        table = replace(
            vi, type="table", required=False, chart_kind=None,
            reason="плотный вариант: числа источника таблицей",
        )
        _attach(result, vi, "table", table)
    # Слияние соседей: слева направо, пока колода больше нижней границы и
    # вместе у пары не больше единиц, чем шаблон вмещает (`room`).
    i = 0
    while i < len(result) - 1 and len(result) > MIN_SLIDES:
        a, b = result[i], result[i + 1]
        if (
            _plain(a) and _plain(b) and a.outline_kind in _MERGEABLE and b.outline_kind in _MERGEABLE
            and a.items + b.items <= most
        ):
            result[i:i + 2] = [_merge(a, b, most)]
        i += 1
    return [
        replace(it, items=min(most, max(DENSE_MIN_ITEMS, it.items)))
        if _plain(it) and it.outline_kind in _LISTY else it
        for it in result
    ]


def _airy(intents: list[SlideIntent]) -> list[SlideIntent]:
    return [replace(it, items=min(AIRY_MAX_ITEMS, it.items)) if _plain(it) else it for it in intents]


def _visual(intents: list[SlideIntent], visuals, allowed=frozenset({"chart", "kpi"})) -> list[SlideIntent]:
    result = list(intents)
    for vi in _unused(result, visuals):
        kind = getattr(vi, "type", None)
        data = getattr(vi, "data", None)
        if "chart" in allowed and (
            kind == "chart" or (kind == "table" and data is not None and data.series is not None)
        ):
            chart = vi if kind == "chart" else replace(
                vi, type="chart", required=False, chart_kind="bar",
                reason="визуальный вариант: числовой ряд графиком",
            )
            _attach(result, vi, "chart", chart)
        elif kind == "kpi" and "kpi" in allowed:
            _attach(result, vi, "kpi", None)
    return [replace(it, items=min(VISUAL_MAX_ITEMS, it.items)) if _plain(it) else it for it in result]


def shape_for_style(intents: list[SlideIntent], style: str, visuals=None, profile=None) -> list[SlideIntent]:
    """Намерения слайдов под стиль `style`. `visuals`: визуалы слоя данных
    по источникам (`plan.data_types.visual_intents`); те, что структура
    уже отдала пунктам, не трогаются. `profile`: шаблон, чтобы dense не
    сливал и не добирал единиц больше, чем уложит хоть одна раскладка."""
    name = getattr(style, "value", style)
    if name == "dense":
        return _dense(list(intents), visuals, room(profile, name), places(profile))
    if name == "airy":
        return _airy(list(intents))
    if name == "visual":
        return _visual(list(intents), visuals, places(profile))
    return list(intents)
