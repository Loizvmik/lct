"""Балл риска слайда: какие слайды показать модели в аудите по картинке.

Полный аудит по картинке стоит вызов модели на слайд (17-46с каждый) и в
пятиминутный бюджет не влезает (замер: 152,8с поверх 302,8с генерации).
Модель нужнее там, где код сам не уверен в результате, поэтому слайды
ранжируются по приметам, которые пайплайн уже знает без модели:

- слайд собран с нуля, а не клоном слайда-примера: у клона вёрстка
  дизайнера, у сборки с нуля своя, и ошибаться ей есть в чём;
- детерминированный аудит нашёл что-то на этом слайде;
- текст близок к вместимости: сборка ужимала кегль или усекала текст;
- на слайде таблица, график или фото: их смысл код не проверяет вовсе;
- автопочинка что-то на слайде меняла.

Функции здесь чистые: ни диска, ни модели, только спецификация слайда и
находки. Веса подобраны так, чтобы одна сильная примета (находка уровня
major, таблица) давала балл около 1, а слайд, чистый по всем приметам,
получал 0 и в аудит не шёл."""
from __future__ import annotations
from typing import Iterable

from deckforge.audit.findings import Finding
from deckforge.plan.spec import DeckSpec, SlideSpec

# Заметку с этой фразой `compose.builder._try_clone` пишет в `SlideSpec.
# findings`, когда слайд принят клоном. Нет заметки: слайд собран с нуля.
_CLONE_NOTE_MARKER = "собран клоном слайда-примера"

# Заметки сборки о том, что содержание не влезло в раскладку: текст ужат,
# усечён или не помещается даже кеглем подписи (`compose.builder._draw_slot`,
# `_fit_cloned_text`), либо блок вовсе «не попал на слайд» без слота под
# свою роль. Живой прогон на VK Education: именно на таких слайдах модель
# находила расхождение заголовка и содержимого (C02, C05).
_CAPACITY_NOTE_MARKERS = ("кегл", "усечен", "усечён", "не помещается", "не влезает", "не попал на слайд")

_SEVERITY_WEIGHT = {"critical": 2.0, "major": 1.0, "minor": 0.3}
# Потолок вклада находок: десяток мелких находок D05/T02 на одном слайде не
# должен перевешивать всё остальное.
_FINDINGS_CAP = 3.0

_FROM_SCRATCH_WEIGHT = 1.0
_CAPACITY_WEIGHT = 1.0
_TABLE_CHART_WEIGHT = 1.0
_PHOTO_WEIGHT = 0.5
_AUTOFIX_WEIGHT = 0.5


def _built_from_scratch(slide: SlideSpec) -> bool:
    return not any(_CLONE_NOTE_MARKER in note for note in slide.findings)


def _near_capacity(slide: SlideSpec) -> bool:
    return any(any(m in note.lower() for m in _CAPACITY_NOTE_MARKERS) for note in slide.findings)


def risk_score(slide_spec: SlideSpec, findings: Iterable[Finding], *, autofixed: bool = False) -> float:
    """Балл риска одного слайда. `findings`: находки детерминированного
    аудита ЭТОГО слайда (вызывающий код отбирает их по `slide_index`);
    `autofixed`: меняла ли автопочинка что-то на слайде."""
    score = 0.0
    if _built_from_scratch(slide_spec):
        score += _FROM_SCRATCH_WEIGHT
    score += min(_FINDINGS_CAP, sum(_SEVERITY_WEIGHT.get(f.severity, 0.0) for f in findings))
    if _near_capacity(slide_spec):
        score += _CAPACITY_WEIGHT
    visual = slide_spec.visual
    if visual is not None:
        if visual.kind in ("table", "chart") or visual.table is not None or visual.chart is not None:
            score += _TABLE_CHART_WEIGHT
        elif visual.kind == "photo" or visual.photo_name:
            score += _PHOTO_WEIGHT
    if autofixed:
        score += _AUTOFIX_WEIGHT
    return score


def pick_risky_slides(
    spec: DeckSpec, findings: Iterable[Finding], *, max_slides: int, min_score: float,
    autofixed_slides: Iterable[int] = (),
) -> list[tuple[int, float]]:
    """До `max_slides` самых рискованных слайдов колоды: список пар
    `(позиция слайда с нуля, балл)` по убыванию балла. Позиция та же, что
    `Finding.slide_index` и порядок PNG-превью, а не `SlideSpec.index`.
    Слайды с баллом ниже `min_score` не берутся: пустой список значит, что
    смотреть модели нечего."""
    by_slide: dict[int, list[Finding]] = {}
    for f in findings:
        if f.slide_index is not None:
            by_slide.setdefault(f.slide_index, []).append(f)
    fixed = set(autofixed_slides)
    scored = [
        (pos, risk_score(slide, by_slide.get(pos, []), autofixed=pos in fixed))
        for pos, slide in enumerate(spec.slides)
    ]
    risky = [(pos, score) for pos, score in scored if score >= min_score]
    risky.sort(key=lambda item: (-item[1], item[0]))
    return risky[:max(0, max_slides)]
