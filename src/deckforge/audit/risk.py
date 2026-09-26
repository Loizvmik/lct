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
получал 0 и в аудит не шёл.

Задача L добавляет вторую, СЕМАНТИЧЕСКУЮ оценку (`semantic_risk`) рядом с
технической (`risk_score`): техническая — про то, как слайд собрался
(клон/с нуля, находки сборки, вместимость), семантическая — про содержание
ДО рендера, по одному `SlideSpec` и его собственным `findings` (заметки
`plan.writer._flag_repeated_headlines`/фолбэка — тот же список строк, что
уже читают `_built_from_scratch`/`_near_capacity` ниже). Обе оценки — не
взаимозаменяемые синонимы «плохого слайда»: технический риск ловит брак
СБОРКИ (не влезло, не тот слот), семантический — брак ЗАМЫСЛА (заголовок —
ярлык темы, а не мысль; цифра без источника; слайд, до которого модель ни
разу не добралась). Аудит по картинке отбирает слайды по СУММЕ обеих
(`pick_risky_slides`)."""
from __future__ import annotations
import re
from typing import Iterable

from deckforge.audit.findings import Finding
from deckforge.plan.spec import BulletBlock, CardBlock, DeckSpec, KpiBlock, SlideSpec, TextBlock

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


# ---------------------------------------------------------------------------
# semantic_risk — семантический риск (задача L): смысл слайда, а не сборка.
# ---------------------------------------------------------------------------

_DIGIT_RE = re.compile(r"\d")

# Заметка `plan.writer._flag_repeated_headlines`: "Слайд N: заголовок похож
# на слайд M (...)" — два заголовка, вероятно, несут один и тот же факт
# другими словами.
_TITLE_DUP_MARKER = "заголовок похож на слайд"

# Заметка `plan.writer._fallback_slide`: "Слайд собран запасным вариантом —
# модель недоступна или не вернула валидный ответ." — содержание не писала
# модель, проверять его смысл code'у не под силу.
_FALLBACK_NOTE_MARKER = "собран запасным вариантом"

# Грубая примета глагола в заголовке (не морфология — эвристика по частым
# окончаниям личных форм и инфинитива): заголовок "Выручка выросла на 30%"
# несёт вывод, "Выручка" — только тему. Ложные срабатывания (не глагол,
# просто похожее окончание) не страшны: это одна из НЕСКОЛЬКИХ примет
# семантического риска, а не единственный судья.
_VERB_HINT_RE = re.compile(
    r"(ть|ться|тся|ет|ют|ят|ит|им|ешь|ишь|ла|ли|ло|ем|ём|уй|йте|ена|ены|ен)\b", re.IGNORECASE,
)

_HEADLINE_TOPIC_WEIGHT = 1.0
_TITLE_DUP_WEIGHT = 1.0
_FALLBACK_WEIGHT = 1.0
_UNSOURCED_NUMBERS_WEIGHT = 1.0
_EMPTY_BLOCKS_WEIGHT = 1.0
_SEMANTIC_CAP = 3.0  # тот же приём, что и `_FINDINGS_CAP`: несколько мелких примет не должны перевесить всё остальное.


def _headline_is_topic(headline: str) -> bool:
    """Заголовок «тема, а не вывод» (бриф задачи L). Совсем короткий (1-2
    слова) почти всегда ярлык раздела («Итоги», «Наша команда»). Заголовок
    подлиннее — риск, только если в нём нет НИ числа, НИ намёка на глагол:
    и то, и другое почти всегда означает готовую мысль, а не подпись темы."""
    words = headline.split()
    if len(words) <= 2:
        return True
    if _DIGIT_RE.search(headline):
        return False
    return not _VERB_HINT_RE.search(headline.lower())


def _has_digits(*texts: str | None) -> bool:
    return any(t and _DIGIT_RE.search(t) for t in texts)


def _slide_has_unsourced_numbers(slide: SlideSpec) -> bool:
    """Есть ли на слайде цифры при отсутствии `source_note`. `plan.spec.
    slide_spec_problems` уже требует `source_note` при наличии цифр НА
    ЭТАПЕ ЗАПИСИ — здесь та же проверка ПОСЛЕ (`apply_variant`/сборка): по
    смыслу это дублирование, но разные слои друг другу не доверяют (то же
    решение, что и остальной код проекта), а после сборки слайд мог
    измениться (дивайдер, запасной путь) в обход валидатора писателя."""
    if slide.source_note and slide.source_note.strip():
        return False
    if _has_digits(slide.headline, slide.subhead):
        return True
    for block in slide.blocks:
        if isinstance(block, TextBlock) and _has_digits(block.text):
            return True
        if isinstance(block, BulletBlock) and any(_has_digits(item) for item in block.items):
            return True
        if isinstance(block, CardBlock) and any(_has_digits(c.body, c.title) for c in block.items):
            return True
        if isinstance(block, KpiBlock) and block.items:
            return True  # KPI по природе несёт число.
    visual = slide.visual
    if visual is not None:
        if visual.table is not None and any(_has_digits(cell) for row in visual.table.rows for cell in row):
            return True
        if visual.chart is not None and visual.chart.series:
            return True  # график по природе несёт числа.
    return False


def semantic_risk(slide_spec: SlideSpec) -> float:
    """Семантический балл риска — считается ДО рендера, только по
    `SlideSpec` (заголовок, блоки, `source_note`) и его собственным
    `findings` (заметки писателя, не находки детерминированного аудита —
    у тех своя роль в `risk_score` выше). Приметы (бриф задачи L):
    заголовок-тема, а не вывод; заголовок похож на другой слайд колоды;
    слайд собран запасным вариантом (модель недоступна/невалидна); цифры
    без источника; пустые блоки (слайд без единого содержательного блока)."""
    score = 0.0
    if _headline_is_topic(slide_spec.headline):
        score += _HEADLINE_TOPIC_WEIGHT
    if any(_TITLE_DUP_MARKER in note for note in slide_spec.findings):
        score += _TITLE_DUP_WEIGHT
    if any(_FALLBACK_NOTE_MARKER in note for note in slide_spec.findings):
        score += _FALLBACK_WEIGHT
    if _slide_has_unsourced_numbers(slide_spec):
        score += _UNSOURCED_NUMBERS_WEIGHT
    if not slide_spec.blocks:
        score += _EMPTY_BLOCKS_WEIGHT
    return min(_SEMANTIC_CAP, score)


_HERO_KINDS = frozenset({"section", "image", "closing"})


def pick_risky_slides(
    spec: DeckSpec, findings: Iterable[Finding], *, max_slides: int, min_score: float,
    autofixed_slides: Iterable[int] = (),
) -> list[tuple[int, float, float]]:
    """До `max_slides` самых рискованных слайдов колоды: список троек
    `(позиция слайда с нуля, технический балл, семантический балл)` по
    убыванию СУММЫ — при равенстве вперёд идёт слайд с большим
    семантическим баллом (бриф задачи L: смысл важнее того, что код и так
    умеет чинить сам). Позиция та же, что `Finding.slide_index` и порядок
    PNG-превью, а не `SlideSpec.index`. Слайды, чья сумма ниже `min_score`,
    не берутся: пустой список значит, что смотреть модели нечего."""
    by_slide: dict[int, list[Finding]] = {}
    for f in findings:
        if f.slide_index is not None:
            by_slide.setdefault(f.slide_index, []).append(f)
    fixed = set(autofixed_slides)
    # Героические слайды (обложка, разделитель, финал) не отправляются:
    # у них по замыслу один заголовок, и модель отвечает «нет содержания»
    # (C05) на каждый такой слайд (живой прогон задачи H, 27 сентября 2026).
    scored = [
        (pos, risk_score(slide, by_slide.get(pos, []), autofixed=pos in fixed), semantic_risk(slide))
        for pos, slide in enumerate(spec.slides)
        if slide.kind not in _HERO_KINDS
    ]
    risky = [(pos, tech, sem) for pos, tech, sem in scored if tech + sem >= min_score]
    risky.sort(key=lambda item: (-(item[1] + item[2]), -item[2], item[0]))
    return risky[:max(0, max_slides)]
