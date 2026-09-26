"""Глобальный планировщик паттернов (раздел 7): раскладка на каждый слайд
назначается сразу всей колоде, до того как написан текст.

Почему глобально. Разнообразие свойство колоды, а не слайда: пошаговый
выбор (`plan.variants`) видел только уже выбранное слева и сажал пять
слайдов из одиннадцати на одну раскладку, потому что для каждого по
отдельности она была лучшей. Лучевой поиск держит `beam_width` частичных
колод и сравнивает их целиком, поэтому может уступить лучшую раскладку
одному слайду, чтобы не повторить её у соседа.

Детерминированно и без модели: одинаковые входы дают одну колоду, поиск на
15 слайдах и 40 раскладках занимает миллисекунды (раздел 7.3)."""
from __future__ import annotations
from dataclasses import dataclass

from deckforge.pattern.candidates import (
    RELAX_TITLES, candidates_for, compatible_kinds, cover_pattern_id, has_fixed_headline, is_closing_pattern,
)
from deckforge.pattern.forms import PatternForm, forms_of
from deckforge.pattern.intent import SlideIntent, intents_from_outline, with_dividers
from deckforge.pattern.scoring import repeat_cost, static_cost
from deckforge.pattern.style import StylePolicy, load_style

DEFAULT_BEAM_WIDTH = 20

# Вид слайда, когда раскладок нет вовсе (пустой профиль): первое
# приближение по смыслу пункта структуры, как раньше у писателя.
_OUTLINE_KIND_TO_SLIDE_KIND: dict[str, str] = {
    "title": "section", "closing": "section", "ask": "section", "divider": "section",
    "agenda": "bullets", "context": "bullets", "problem": "bullets", "risks": "bullets",
    "solution": "two_col", "comparison": "two_col",
    "how_it_works": "cards", "case": "cards", "team": "cards", "roadmap": "cards",
    "data": "kpi",
}


@dataclass(frozen=True)
class PatternAssignment:
    """Раскладка одного слайда колоды. `position`: номер в итоговой колоде
    (у airy с разделителями он не совпадает с номером пункта структуры).
    `relaxed`: какие жёсткие ограничения пришлось ослабить (бедный
    шаблон), для честной находки."""
    position: int
    intent: SlideIntent
    pattern_id: str | None
    kind: str
    cost: float = 0.0
    relaxed: tuple[str, ...] = ()

    def gap_note(self) -> str | None:
        if not self.relaxed:
            return None
        why = "; ".join(RELAX_TITLES.get(r, r) for r in self.relaxed)
        return (
            f"Слайд {self.position}: раскладка под содержание не найдена — {why}; "
            f"взята {self.pattern_id or 'никакая'} ({self.kind})."
        )


@dataclass
class _Beam:
    cost: float
    ids: tuple[str, ...]
    uses: dict[str, int]


def _has_divider_layout(profile, cover_id: str | None, closing_ids: frozenset[str]) -> bool:
    return any(
        p.kind in ("section", "closing") and p.pattern_id != cover_id and p.pattern_id not in closing_ids
        and not has_fixed_headline(p)
        for p in profile.patterns
    )


def _as_intents(outline_or_intents) -> list[SlideIntent]:
    if isinstance(outline_or_intents, list):
        return list(outline_or_intents)
    return intents_from_outline(outline_or_intents)


def plan_patterns(
    outline, profile, style, *, beam_width: int = DEFAULT_BEAM_WIDTH, policy: StylePolicy | None = None,
) -> list[PatternAssignment]:
    """Раскладка на каждый слайд колоды стиля `style`. `outline`:
    `plan.outline.Outline` или готовый список `SlideIntent` (с фото).
    Разделители airy добавляются здесь: им тоже нужна раскладка, и повтор
    у них считается наравне с остальными."""
    policy = policy or load_style(style)
    intents = _as_intents(outline)
    cover_id = cover_pattern_id(profile)
    closing_ids = frozenset(p.pattern_id for p in profile.patterns if is_closing_pattern(p, profile))
    if policy.dividers and _has_divider_layout(profile, cover_id, closing_ids):
        # Без героической раскладки разделитель сел бы на карточки или
        # колонки и выглядел бы недописанным слайдом (ЛЦТ2026: героических
        # раскладок, кроме обложки, нет).
        intents = with_dividers(intents)
    if not intents:
        return []
    patterns = {p.pattern_id: p for p in profile.patterns}
    if not patterns:
        return [
            PatternAssignment(
                position=i, intent=it, pattern_id=None,
                kind=_OUTLINE_KIND_TO_SLIDE_KIND.get(it.outline_kind, "bullets"),
            )
            for i, it in enumerate(intents)
        ]

    forms = forms_of(profile)
    last = len(intents) - 1

    per_slide: list[list[tuple[str, float]]] = []
    relaxed: list[tuple[str, ...]] = []
    for position, intent in enumerate(intents):
        found = candidates_for(
            intent, profile, forms, position=position, last=last, cover_id=cover_id, closing_ids=closing_ids,
        )
        scored = [
            (pid, static_cost(
                intent, patterns[pid], forms[pid], policy, position=position, last=last,
                cover_id=cover_id, closing_ids=closing_ids,
            ))
            for pid in found.pattern_ids
        ]
        scored.sort(key=lambda pair: (pair[1], pair[0]))
        per_slide.append(scored)
        relaxed.append(found.relaxed)

    beams = [_Beam(cost=0.0, ids=(), uses={})]
    width = max(1, beam_width)
    for scored in per_slide:
        expanded: dict[tuple, _Beam] = {}
        for beam in beams:
            previous = beam.ids[-1] if beam.ids else None
            for pid, cost in scored:
                total = beam.cost + cost + repeat_cost(pid, previous, beam.uses.get(pid, 0), policy)
                uses = dict(beam.uses)
                uses[pid] = uses.get(pid, 0) + 1
                ids = (*beam.ids, pid)
                # Две частичные колоды с одним последним слайдом и одним
                # набором использованных раскладок дальше неразличимы для
                # стоимости: оставляется дешёвая, и луч не забивается
                # перестановками одного и того же.
                key = (pid, tuple(sorted(uses.items())))
                kept = expanded.get(key)
                if kept is None or (total, ids) < (kept.cost, kept.ids):
                    expanded[key] = _Beam(cost=total, ids=ids, uses=uses)
        beams = sorted(expanded.values(), key=lambda b: (round(b.cost, 9), b.ids))[:width]

    best = beams[0]
    static = [dict(scored) for scored in per_slide]
    return [
        PatternAssignment(
            position=i, intent=intent, pattern_id=pid, kind=patterns[pid].kind,
            cost=round(static[i][pid], 3), relaxed=relaxed[i],
        )
        for i, (intent, pid) in enumerate(zip(intents, best.ids))
    ]


def repick_pattern(
    slide, position: int, ids: list[str | None], profile, style, *, policy: StylePolicy | None = None,
) -> str | None:
    """Раскладка для одного слайда, чьё содержание после письма поменяло
    форму (`plan.normalize`: одна карточка стала абзацем, два пункта с
    числами показателями). Соседи уже назначены (`ids`), их не трогаем:
    кандидаты по виду нового содержания, стоимость та же, что у
    планировщика, повтор считается против соседей слева и справа."""
    if profile is None or not profile.patterns:
        return None
    policy = policy or load_style(style)
    forms: dict[str, PatternForm] = forms_of(profile)
    last = len(ids) - 1
    cover_id = cover_pattern_id(profile)
    closing_ids = frozenset(p.pattern_id for p in profile.patterns if is_closing_pattern(p, profile))
    kinds = compatible_kinds(slide, profile)
    units = sum(len(getattr(b, "items", None) or [None]) for b in slide.blocks)
    intent = SlideIntent(index=position, outline_kind="context", intent=slide.headline, items=units)
    others = [pid for i, pid in enumerate(ids) if i != position and pid]
    neighbours = {ids[i] for i in (position - 1, position + 1) if 0 <= i <= last and ids[i]}
    best: tuple[float, str] | None = None
    for p in profile.patterns:
        if p.kind not in kinds:
            continue
        if (p.pattern_id in closing_ids and position != last) or (p.pattern_id == cover_id and position != 0):
            continue
        if has_fixed_headline(p) and position != last:
            continue
        cost = static_cost(
            intent, p, forms[p.pattern_id], policy, position=position, last=last,
            cover_id=cover_id, closing_ids=closing_ids,
        )
        cost += policy.weight("consecutive_repeat") * (p.pattern_id in neighbours)
        cost += policy.weight("repeated_pattern") * others.count(p.pattern_id)
        if best is None or (cost, p.pattern_id) < best:
            best = (cost, p.pattern_id)
    return best[1] if best is not None else None
