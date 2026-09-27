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
from collections.abc import Mapping
from dataclasses import dataclass, replace

from deckforge.pattern.candidates import (
    RELAX_TITLES, candidates_for, compatible_kinds, cover_pattern_id, has_fixed_headline, not_plain_content,
    is_closing_pattern, is_cover_like,
)
from deckforge.pattern.forms import PatternForm, forms_of
from deckforge.pattern.intent import SlideIntent, intents_from_outline, with_dividers
from deckforge.pattern.scoring import growing_repeat_cost, look_key, overflow, static_cost
from deckforge.pattern.style import StylePolicy, load_style

DEFAULT_BEAM_WIDTH = 20

# Запасных раскладок на слайд. Сборка пробует клон каждой (миллисекунды),
# но дальше третьей-четвёртой по стоимости идут раскладки, которые
# планировщик и так счёл плохими для этого содержания.
MAX_ALTERNATIVES = 3

# Доля содержания, которой раскладка не вмещает (`scoring.overflow`), выше
# которой раскладка тесна слайду и альтернативой для разнообразия не
# считается: половина слов стиля на единицу не ложится кеглем примера.
TIGHT_OVERFLOW = 0.5

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
    # Запасные раскладки того же вида в порядке стоимости, без выбранной:
    # вторая ступень лестницы сборки (`compose.builder`), если клон
    # выбранной отклонён. Порядок тот же, каким планировщик их оценивал,
    # чтобы сборка не подбирала замену своим, иным расчётом.
    alternatives: tuple[str, ...] = ()
    # Форма, которую сборка строит с нуля на этой раскладке-носителе
    # (`candidates.SCRATCH_FORMS`): у шаблона нет раскладки её вида.
    scratch_form: str | None = None

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
    # Сколько последних слайдов подряд одного облика и одного вида: для
    # ограничений разнообразия (`StylePolicy.diversity`).
    look_run: int = 0
    kind_run: int = 0


def _violates(beam: _Beam, look: str, kind: str, previous_look: str | None, previous_kind: str | None,
              policy: StylePolicy) -> bool:
    """Нарушит ли раскладка облика `look` и вида `kind` ограничения
    разнообразия колоды, если поставить её следующей за `beam`."""
    rules = policy.diversity
    if beam.uses.get(look, 0) + 1 > rules.same_pattern_max_total:
        return True
    run = beam.look_run + 1 if look == previous_look else 1
    if run > rules.same_pattern_max_consecutive:
        return True
    limit = rules.kind_limit(kind)
    kind_run = beam.kind_run + 1 if kind == previous_kind else 1
    return limit is not None and kind_run > limit


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


def _batch_penalty(intent: SlideIntent, look: str, taken: Mapping[int, set[str]] | None, policy: StylePolicy) -> float:
    """Штраф за облик, который сосед по пакету уже поставил на тот же пункт
    содержания (задача D3). Героические слайды не штрафуются: обложка и
    финал заданы шаблоном, и совпадать им положено. Слайды с графиком или
    таблицей тоже: место под них выбирает качество места
    (`scoring.chart_fit`), и отнять у visual родной график примера ради
    различия значило бы показать числа хуже."""
    if not taken or intent.is_hero or intent.required_visual in ("chart", "table"):
        return 0.0
    if any(look in taken.get(i, ()) for i in intent.outline_indices):
        return policy.weight("batch_overlap")
    return 0.0


def plan_patterns(
    outline, profile, style, *, beam_width: int = DEFAULT_BEAM_WIDTH, policy: StylePolicy | None = None,
    taken: Mapping[int, set[str]] | None = None,
) -> list[PatternAssignment]:
    """Раскладка на каждый слайд колоды стиля `style`. `outline`:
    `plan.outline.Outline` или готовый список `SlideIntent` (с фото).
    Разделители airy добавляются здесь: им тоже нужна раскладка, и повтор
    у них считается наравне с остальными.

    `taken`: облики раскладок, которые соседние стили пакета уже назначили
    пунктам структуры (номер пункта -> облики, `batch_looks`). Совпадение
    по пункту штрафуется весом `batch_overlap`: три варианта одного
    содержания обязаны различаться глазом, а не только словами."""
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
    scratch: list[str | None] = []
    # Раскладки, где содержанию слайда хватает места (`scoring.overflow`
    # ниже `TIGHT_OVERFLOW`): только они считаются альтернативой, ради
    # которой разнообразие запрещает повтор.
    roomy: list[set[str]] = []
    looks = {pid: look_key(p) for pid, p in patterns.items()}
    for position, intent in enumerate(intents):
        found = candidates_for(
            intent, profile, forms, position=position, last=last, cover_id=cover_id, closing_ids=closing_ids,
        )
        # Носитель формы с нуля оценивается как слайд без единиц: место под
        # форму считает сборка, а вместимость текстового места носителя к
        # показателям или таблице отношения не имеет.
        costed = replace(intent, items=0, photo=None) if found.scratch_form else intent
        scored = [
            (pid, static_cost(
                costed, patterns[pid], forms[pid], policy, position=position, last=last,
                cover_id=cover_id, closing_ids=closing_ids,
            ) + _batch_penalty(intent, looks[pid], taken, policy))
            for pid in found.pattern_ids
        ]
        scored.sort(key=lambda pair: (pair[1], pair[0]))
        per_slide.append(scored)
        roomy.append({pid for pid in found.pattern_ids if overflow(costed, forms[pid], policy) < TIGHT_OVERFLOW})
        relaxed.append(found.relaxed)
        scratch.append(found.scratch_form)

    # Повтор считается по облику раскладки, а не по `pattern_id` (задача
    # V2, `scoring.look_key`): два примера с одной геометрией для глаза одна
    # раскладка.
    beams = [_Beam(cost=0.0, ids=(), uses={})]
    width = max(1, beam_width)
    for scored, fits in zip(per_slide, roomy):
        expanded: dict[tuple, _Beam] = {}
        for beam in beams:
            previous = looks[beam.ids[-1]] if beam.ids else None
            previous_kind = patterns[beam.ids[-1]].kind if beam.ids else None
            # Ограничения разнообразия: запрет, но только если у этого
            # слайда есть просторная альтернатива, которая их не нарушает.
            # Тесная раскладка альтернативой не считается (задача V5: с
            # честным замером клона таймлайн примера 42 держит в событии
            # два слова, и запрет повтора сажал на него содержательные
            # слайды dense). Нет просторной без нарушения: выбирают
            # стоимости, где повтор дорожает с каждым разом, а теснота
            # стоит как переполнение.
            free = [
                (pid, cost) for pid, cost in scored
                if not _violates(beam, looks[pid], patterns[pid].kind, previous, previous_kind, policy)
            ]
            allowed = [pair for pair in free if pair[0] in fits] or (scored if fits else free or scored)
            for pid, cost in allowed:
                look = looks[pid]
                kind = patterns[pid].kind
                total = beam.cost + cost + growing_repeat_cost(look, previous, beam.uses.get(look, 0), policy)
                uses = dict(beam.uses)
                uses[look] = uses.get(look, 0) + 1
                look_run = beam.look_run + 1 if look == previous else 1
                kind_run = beam.kind_run + 1 if kind == previous_kind else 1
                ids = (*beam.ids, pid)
                # Две частичные колоды с одним последним слайдом и одним
                # набором использованных раскладок дальше неразличимы для
                # стоимости: оставляется дешёвая, и луч не забивается
                # перестановками одного и того же.
                key = (look, kind_run, tuple(sorted(uses.items())))
                kept = expanded.get(key)
                if kept is None or (total, ids) < (kept.cost, kept.ids):
                    expanded[key] = _Beam(cost=total, ids=ids, uses=uses, look_run=look_run, kind_run=kind_run)
        beams = sorted(expanded.values(), key=lambda b: (round(b.cost, 9), b.ids))[:width]

    best = beams[0]
    static = [dict(scored) for scored in per_slide]
    return [
        PatternAssignment(
            position=i, intent=intent, pattern_id=pid, kind=patterns[pid].kind,
            cost=round(static[i][pid], 3), relaxed=relaxed[i],
            alternatives=_same_form_alternatives(per_slide[i], pid, patterns, forms, carrier=bool(scratch[i])),
            scratch_form=scratch[i],
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
    other_looks = {q.pattern_id: look_key(q) for q in profile.patterns}
    neighbour_looks = {other_looks[pid] for pid in neighbours if pid in other_looks}
    best: tuple[float, str] | None = None
    for p in profile.patterns:
        if p.kind not in kinds:
            continue
        if (p.pattern_id in closing_ids and position != last) or (p.pattern_id == cover_id and position != 0):
            continue
        if has_fixed_headline(p) and position != last:
            continue
        if is_cover_like(p, profile) and position not in (0, last):
            continue
        if not_plain_content(forms[p.pattern_id]) and not _has_chart(slide):
            # Место под график без графика пустеет: картинку-график и
            # данные-образец родного графика сборка удаляет (задача V1).
            continue
        cost = static_cost(
            intent, p, forms[p.pattern_id], policy, position=position, last=last,
            cover_id=cover_id, closing_ids=closing_ids,
        )
        look = look_key(p)
        cost += policy.weight("consecutive_repeat") * (look in neighbour_looks)
        uses = sum(1 for other in others if other_looks.get(other) == look)
        cost += growing_repeat_cost(look, None, uses, policy)
        if best is None or (cost, p.pattern_id) < best:
            best = (cost, p.pattern_id)
    return best[1] if best is not None else None


def _same_form_alternatives(
    scored: list[tuple[str, float]], chosen: str, patterns: dict, forms: dict[str, PatternForm],
    *, carrier: bool = False,
) -> tuple[str, ...]:
    """Запасные раскладки выбранной: того же вида И той же главной формы
    (карточки, список, показатели). Одного вида мало: у VK Education вид
    `image` у диаграммы Ганта (пять карточек) и у «Паттерн + фото» (один
    абзац). Карточки, написанные под Гант, клон запасной раскладки терял
    целиком, а в тело ложилась подпись месяца «Май» (задача V2, 27
    сентября 2026)."""
    def block(pid: str) -> str | None:
        main = forms[pid].main
        return main.block if main is not None else None

    if carrier:
        # Носителю формы с нуля годится любой другой носитель: форма у них
        # общая (строит сборка), а место под неё сборка мерит сама.
        return tuple(other for other, _cost in scored if other != chosen)[:MAX_ALTERNATIVES]
    want_kind, want_block = patterns[chosen].kind, block(chosen)
    return tuple(
        other for other, _cost in scored
        if other != chosen and patterns[other].kind == want_kind and block(other) == want_block
    )[:MAX_ALTERNATIVES]
def _has_chart(slide) -> bool:
    visual = getattr(slide, "visual", None)
    return visual is not None and visual.kind == "chart" and visual.chart is not None


def batch_looks(assignments: list[PatternAssignment], profile) -> dict[int, set[str]]:
    """Облики раскладок колоды по пунктам структуры: вход `taken` для
    следующего стиля пакета. Героические слайды не входят: им совпадать
    положено (`_batch_penalty`)."""
    patterns = {p.pattern_id: p for p in profile.patterns}
    out: dict[int, set[str]] = {}
    for a in assignments:
        if a.intent.is_hero or a.pattern_id not in patterns:
            continue
        look = look_key(patterns[a.pattern_id])
        for i in a.intent.outline_indices:
            out.setdefault(i, set()).add(look)
    return out


def merge_looks(*maps: Mapping[int, set[str]]) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for m in maps:
        for i, looks in m.items():
            out.setdefault(i, set()).update(looks)
    return out


# Порядок планирования стилей пакета: каждый следующий видит облики
# предыдущих. Плотный первым: у него меньше всего свободы (таблицы и
# колонки под много единиц), визуальный последним: у него больше всего
# нарядных раскладок на выбор.
BATCH_ORDER = ("dense", "airy", "visual")


def plan_batch(
    intents_by_style: Mapping[str, list[SlideIntent]], profile, *, beam_width: int = DEFAULT_BEAM_WIDTH,
) -> dict[str, list[PatternAssignment]]:
    """Раскладки для нескольких стилей одного пакета, по очереди
    `BATCH_ORDER`: каждый стиль штрафуется за облики соседей на тех же
    пунктах. Для CLI и тестов; API делает то же по заданиям
    (`api.jobs.PlanShare`)."""
    order = [s for s in BATCH_ORDER if s in intents_by_style]
    order += [s for s in intents_by_style if s not in order]
    taken: dict[int, set[str]] = {}
    out: dict[str, list[PatternAssignment]] = {}
    for style in order:
        out[style] = plan_patterns(intents_by_style[style], profile, style, beam_width=beam_width, taken=taken)
        taken = merge_looks(taken, batch_looks(out[style], profile))
    return out
