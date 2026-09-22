"""Пишет текст слайдов по структуре (`outline.Outline`) и подбирает КОНКРЕТНУЮ
раскладку шаблона под уже написанное содержание — вторая и третья ступени
планирования Task 13 (первая — `outline.build_outline`).

Два публичных интерфейса брифа:

- `write_slides(outline, sources, profile, llm) -> DeckSpec` — по одному
  вызову модели (`agents/slide-writer/AGENT.md`) на пункт структуры;
  лимиты длины содержания приходят из ЗАМЕРЕННОЙ вместимости раскладки
  шаблона (`Capacity`, `template/patterns.py`), не из головы (брифом,
  "Требования к работе"). Каждый написанный слайд проверяется `plan.spec.
  slide_spec_problems` немедленно — невалидный слайд (опечатка поля, пустой
  текст, цифра без источника) не портит всю колоду молча: код просит модель
  исправить один раз, а если и это не помогло — берёт детерминированный
  запасной вариант с честной пометкой в `SlideSpec.findings` (тот же
  принцип "модель предлагает, код проверяет и не падает", что и
  `template/naming.py`).
- `pick_patterns(deck_spec, profile, llm) -> DeckSpec` — по одному вызову
  модели (`agents/pattern-picker/AGENT.md`) на слайд, ТОЛЬКО среди
  паттернов, УЖЕ отобранных кодом по `SlideSpec.kind` (модель физически не
  может предложить раскладку вне шаблона — её ей не показывают); ответ вне
  списка кандидатов код отвергает и берёт раскладку по вместимости сам
  (`_best_by_capacity`, тот же критерий, что и `compose.builder._pick_
  pattern` использует геометрически точнее — здесь, в `plan/`, доступны
  только числа `Capacity`, не координаты `Box`, тем самым план по-прежнему
  не знает ни одной координаты).
"""
from __future__ import annotations
import json
import re
from pathlib import Path

import yaml

from deckforge.plan.outline import Outline, SourceDoc
from deckforge.plan.spec import (
    SLIDE_KINDS, DeckSpec, SlideSpec, slide_spec_from_dict, slide_spec_problems,
)
from deckforge.provider.base import LLMProvider

AGENT_PATH_WRITER = Path(__file__).resolve().parents[3] / "agents" / "slide-writer" / "AGENT.md"
AGENT_PATH_PICKER = Path(__file__).resolve().parents[3] / "agents" / "pattern-picker" / "AGENT.md"

# Живой прогон обязательной проверки задачи (девять презентаций, отчёт
# задачи): на старте 3072 эскалация до потолка `MAX_TOKENS_BUDGET_CAP`=6144
# (`provider/yandex.py`) срабатывала практически на каждом вызове
# slide-writer — та же находка и то же решение, что и у `outline.
# OUTLINE_MAX_TOKENS` (см. её комментарий): начинать ниже гарантированно
# нужного бюджета только теряет время на лишний HTTP-круг.
WRITER_MAX_TOKENS = 6144
PICKER_MAX_TOKENS = 2048

# Запасная вместимость для `kind`, которого нет вовсе ни в одном паттерне
# профиля (шаблон бедный, или тестовая синтетика) — round-number, того же
# порядка, что и типичные измеренные значения на трёх учебных шаблонах
# (`tests/template/test_patterns.py`), не подгонка под конкретный файл.
_DIGIT_RE = re.compile(r"\d")

_FALLBACK_CAPACITY = {
    "max_items": 4, "max_chars_per_item": 140, "max_bullets": 6,
    "max_series": 4, "max_rows": 6, "max_cols": 4,
}

# Запасной лимит длины заголовка, когда ни один паттерн `kind` не несёт
# слота роли "headline" с измеренной `max_chars` (не должно случаться на
# `SLIDE_KINDS`, где headline обязателен всем, кроме "section"/"image" —
# `_HEADLINE_EXEMPT_KINDS`, `template/patterns.py`; честный запасной вариант
# на случай вырожденного профиля). Округлая величина того же порядка, что
# типичный заголовок-вывод на одну-две строки, не подгонка под файл.
_FALLBACK_HEADLINE_CHARS = 70

# Пункт структуры (`OutlineSlide.kind`, словарь outline-writer) -> желаемый
# `SlideSpec.kind` (закрытые семь значений `plan.spec.SLIDE_KINDS`) —
# грубое, но детерминированное первое приближение вёрстки, нужное ДО того,
# как известна конкретная раскладка (`pick_patterns`/`variants.apply_
# variant` идут следующими шагами): slide-writer обязан знать примерный
# лимит длины текста уже сейчас, а лимит приходит из вместимости раскладки
# ИМЕННО этого `kind` (см. `_kind_capacity`). Сам `kind` в ответе модели
# может отличаться от этой подсказки (AGENT.md разрешает это явно), если
# контент содержательно не ложится в предложенный тип.
_OUTLINE_KIND_TO_SLIDE_KIND: dict[str, str] = {
    "title": "section", "closing": "section", "ask": "section",
    "agenda": "bullets", "context": "bullets", "problem": "bullets", "risks": "bullets",
    "solution": "two_col", "comparison": "two_col",
    "how_it_works": "cards", "case": "cards", "team": "cards", "roadmap": "cards",
    "data": "kpi",
}


def _load_agent_prompt(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path}: ожидался YAML-фронтматтер, ограниченный `---`")
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


def _kind_capacity(kind: str, profile) -> dict:
    """Вместимость раскладки `kind` в ЭТОМ шаблоне — ориентир длины текста
    для slide-writer.

    Раньше здесь был МИНИМУМ по всем намайненным паттернам `kind`
    (консервативная оценка "валидна для любого паттерна этого kind") — живой
    прогон обязательной проверки задачи вскрыл, что это не работает: разброс
    `max_chars_per_item` у раскладок ОДНОГО `kind` в реальном шаблоне велик
    (VK Tech, `cards`: от 14 до 252 знаков на элемент), и минимум по всем
    четырём (14) делает лимит практически бесполезным — контент такой длины
    не несёт ни одного факта из источников. Число нужно не для того, чтобы
    ГАРАНТИРОВАННО подойти под любую раскладку `kind` (эту работу теперь
    делает `plan.variants._pick_pattern_id`, сверяясь с ФАКТИЧЕСКОЙ длиной
    написанного текста, не только с числом элементов — см. её докстроку), а
    чтобы дать модели разумный ПОТОЛОК: МАКСИМУМ среди паттернов `kind`
    — content, вписавшийся в самую вместительную раскладку этого `kind`,
    подбор паттерна дальше сам найдёт (или, если ни одна не хватит, сборка
    честно ужмёт/усечёт и оставит finding — тот же путь, каким `compose.
    builder` уже обрабатывает любое расхождение содержания с раскладкой)."""
    candidates = [p.capacity for p in profile.patterns if p.kind == kind]
    if not candidates:
        return dict(_FALLBACK_CAPACITY)

    def _max_positive(values: list[int], default: int) -> int:
        positive = [v for v in values if v > 0]
        return max(positive) if positive else default

    return {
        "max_items": _max_positive([c.max_items for c in candidates], _FALLBACK_CAPACITY["max_items"]),
        "max_chars_per_item": _max_positive(
            [c.max_chars_per_item for c in candidates], _FALLBACK_CAPACITY["max_chars_per_item"],
        ),
        "max_bullets": _max_positive([c.max_bullets for c in candidates], _FALLBACK_CAPACITY["max_bullets"]),
        "max_series": _max_positive([c.max_series for c in candidates], _FALLBACK_CAPACITY["max_series"]),
        "max_rows": _max_positive([c.max_rows for c in candidates], _FALLBACK_CAPACITY["max_rows"]),
        "max_cols": _max_positive([c.max_cols for c in candidates], _FALLBACK_CAPACITY["max_cols"]),
    }


def _headline_capacity(kind: str, profile) -> int:
    """Лимит длины ЗАГОЛОВКА — живой прогон обязательной проверки задачи
    (девять презентаций, отчёт задачи) вскрыл, что `_kind_capacity` выше
    ограничивает длину body/bullet/card-содержания, но НЕ headline —
    slide-writer писал заголовок-вывод ПОЛНЫМ предложением (60-100+ знаков),
    который на живом рендере переносился на 2-3 строки и наезжал на
    содержание ниже (находки L02/L03 аудита). Тот же принцип, что и у
    `_kind_capacity` (МАКСИМУМ среди раскладок `kind`, не минимум — см. её
    докстроку), взятый по слоту роли `"headline"` вместо `body`/`card_body`/
    `bullet`."""
    lengths = [
        s.max_chars
        for p in profile.patterns if p.kind == kind
        for s in p.slots if s.role == "headline" and s.max_chars > 0
    ]
    return max(lengths) if lengths else _FALLBACK_HEADLINE_CHARS


def _fallback_slide(kind: str, index: int, intent: str, needs: list[str]) -> SlideSpec:
    """Детерминированный запасной слайд — используется без модели и когда
    модель дважды не смогла вернуть валидный слайд. Заведомо проходит
    `slide_spec_problems` — колода собирается целиком, с честной пометкой в
    `findings`, а не падает на одном плохом слайде.

    `intent` (структура от `outline.build_outline`) — свободный текст и
    МОЖЕТ нести цифру (например, "Обучение 340 согласующих") — тогда
    `slide_spec_problems`/`validate_deck_spec` потребуют `source_note`
    (правило "цифра без источника" не делает исключения для запасного
    варианта). Настоящего источника у запасного варианта нет по
    определению (модель не писала текст) — `source_note` честно говорит об
    этом словами, а не выдумывает ссылку на данные."""
    headline = intent.strip() or "Слайд требует содержания"
    finding = "Слайд собран запасным вариантом — модель недоступна или не вернула валидный ответ."
    if needs:
        finding += f" Нужны данные: {', '.join(needs)}."
    source_note = "Источник не подтверждён — текст запасного варианта, требует проверки перед показом." \
        if _DIGIT_RE.search(headline) else None
    return SlideSpec(index=index, kind=kind, headline=headline, source_note=source_note, findings=[finding])


_SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(SLIDE_KINDS)},
        "headline": {"type": "string"},
        "subhead": {"type": "string"},
        "blocks": {"type": "array"},
        "visual": {"type": "object"},
        "source_note": {"type": "string"},
        "speaker_notes": {"type": "string"},
    },
    "required": ["kind", "headline"],
    "additionalProperties": False,
}


def _ask_slide_writer(prompt_body: str, payload: dict, index: int, llm: LLMProvider, *, repair: list[str] | None = None) -> SlideSpec | None:
    user_payload = dict(payload)
    if repair:
        user_payload["previous_answer_problems"] = repair
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    try:
        raw = llm.complete(messages, schema=_SLIDE_SCHEMA, max_tokens=WRITER_MAX_TOKENS)
        data = json.loads(raw)
        slide = slide_spec_from_dict(data, index)
    except Exception:
        return None
    return slide


def write_slides(outline: Outline, sources: list[SourceDoc], profile, llm: LLMProvider | None) -> DeckSpec:
    _meta, prompt_body = _load_agent_prompt(AGENT_PATH_WRITER)
    source_text = "\n\n".join(f"### {s.name}\n{s.text}" for s in sources)

    slides: list[SlideSpec] = []
    for index, item in enumerate(outline.slides):
        desired_kind = _OUTLINE_KIND_TO_SLIDE_KIND.get(item.kind, "bullets")
        capacity = _kind_capacity(desired_kind, profile)
        payload = {
            "slide_kind_hint": item.kind,
            "intent": item.intent,
            "needs": item.needs,
            "layout_kind": desired_kind,
            "capacity": capacity,
            "max_headline_chars": _headline_capacity(desired_kind, profile),
            "sources": source_text,
            "position": {"index": index, "total": len(outline.slides)},
        }

        slide: SlideSpec | None = None
        if llm is not None:
            slide = _ask_slide_writer(prompt_body, payload, index, llm)
            if slide is not None:
                problems = slide_spec_problems(slide)
                if problems:
                    # Один шанс на исправление — код показывает модели её
                    # собственные ошибки (брифом: "валидатор ловит то, что
                    # иначе всплывёт при сборке" — здесь оно ловится ДО
                    # сборки и ДО того, как испортит остальную колоду).
                    repaired = _ask_slide_writer(prompt_body, payload, index, llm, repair=problems)
                    slide = repaired if repaired is not None and not slide_spec_problems(repaired) else None

        if slide is None:
            slide = _fallback_slide(desired_kind, index, item.intent, item.needs)

        slides.append(slide)

    return DeckSpec(title=outline.title, language=outline.language, slides=slides)


# ---------------------------------------------------------------------------
# pick_patterns — подбор КОНКРЕТНОЙ раскладки моделью, поверх уже
# написанного содержания.
# ---------------------------------------------------------------------------

_PICKER_SCHEMA = {
    "type": "object",
    "properties": {"pattern_id": {"type": "string"}},
    "required": ["pattern_id"],
    "additionalProperties": False,
}


def _content_item_count(slide: SlideSpec) -> int | None:
    """Фактический объём содержания слайда — то же число, что `compose.
    builder._repeat_item_count` использует для геометрически точного
    подбора, здесь взято проще (без повторов раскладки, `plan/` их не
    знает): число буллетов/карточек/KPI/строк таблицы/рядов графика, в
    зависимости от того, что на слайде есть. `None` — на слайде нет
    контента, объём которого имеет смысл сравнивать с `Capacity.max_items`
    (например, чистый текстовый абзац)."""
    from deckforge.plan.spec import BulletBlock, CardBlock, KpiBlock

    for block in slide.blocks:
        if isinstance(block, CardBlock) and block.items:
            return len(block.items)
        if isinstance(block, BulletBlock) and block.items:
            return len(block.items)
        if isinstance(block, KpiBlock) and block.items:
            return len(block.items)
    if slide.visual is not None and slide.visual.table is not None and slide.visual.table.rows:
        return len(slide.visual.table.rows) - 1  # без шапки
    return None


def _best_by_capacity(candidates: list, item_count: int | None):
    """Раскладка того же `kind`, чья `Capacity.max_items` ближе всего к
    фактическому объёму содержания — код-фолбэк `pick_patterns`, когда
    модель недоступна или предложила `pattern_id` вне списка кандидатов;
    тот же критерий, что первым делом ранжирует `compose.builder._pick_
    pattern` (`_capacity_badness`), выраженный только через `Capacity`
    (без `Box`/`fits()` — `plan/` не знает координат и не меряет текст,
    это работа `compose/`, которая при сборке ВСЁ РАВНО перепроверит
    итоговый выбор через `fits()` и при необходимости ужмёт/подвинет)."""
    if not candidates:
        return None
    if item_count is None:
        return max(candidates, key=lambda p: p.score)

    def badness(p):
        max_items = p.capacity.max_items or 1
        return (abs(max_items - item_count) / max_items, -p.score)

    return min(candidates, key=badness)


def pick_patterns(deck_spec: DeckSpec, profile, llm: LLMProvider | None) -> DeckSpec:
    """Проставляет `SlideSpec.pattern_id` — по одному вызову модели на
    слайд, среди паттернов ТОЛЬКО того `kind`, что уже несёт слайд (модель
    не может предложить раскладку вне шаблона — ей показывают только
    список кандидатов этого `kind`); без модели, при сбое вызова или на
    ответе вне списка кандидатов — код сам берёт раскладку по вместимости
    (`_best_by_capacity`), а не оставляет `pattern_id` пустым."""
    _meta, prompt_body = _load_agent_prompt(AGENT_PATH_PICKER) if llm is not None else ({}, "")

    new_slides: list[SlideSpec] = []
    for slide in deck_spec.slides:
        candidates = [p for p in profile.patterns if p.kind == slide.kind]
        if not candidates:
            new_slides.append(slide)
            continue

        item_count = _content_item_count(slide)
        fallback = _best_by_capacity(candidates, item_count)
        chosen_id = fallback.pattern_id if fallback is not None else None

        if llm is not None:
            payload = {
                "slide": {
                    "kind": slide.kind, "headline": slide.headline,
                    "item_count": item_count,
                },
                "candidates": [
                    {
                        "pattern_id": p.pattern_id,
                        "capacity": {
                            "max_items": p.capacity.max_items,
                            "max_chars_per_item": p.capacity.max_chars_per_item,
                            "max_bullets": p.capacity.max_bullets,
                            "max_series": p.capacity.max_series,
                            "max_rows": p.capacity.max_rows,
                            "max_cols": p.capacity.max_cols,
                        },
                        "decor_count": len(p.decor),
                        "score": p.score,
                    }
                    for p in candidates
                ],
            }
            messages = [
                {"role": "system", "content": prompt_body},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            try:
                raw = llm.complete(messages, schema=_PICKER_SCHEMA, max_tokens=PICKER_MAX_TOKENS)
                data = json.loads(raw)
                proposed = data.get("pattern_id")
                valid_ids = {p.pattern_id for p in candidates}
                if isinstance(proposed, str) and proposed in valid_ids:
                    chosen_id = proposed
                # иначе — модель предложила раскладку вне списка (или сбой
                # разбора) — код отвергает и оставляет уже посчитанный fallback
            except Exception:
                pass

        new_slides.append(
            SlideSpec(
                index=slide.index, kind=slide.kind, headline=slide.headline, subhead=slide.subhead,
                blocks=slide.blocks, visual=slide.visual, source_note=slide.source_note,
                speaker_notes=slide.speaker_notes, findings=list(slide.findings), pattern_id=chosen_id,
            )
        )

    return DeckSpec(title=deck_spec.title, language=deck_spec.language, slides=new_slides, meta=dict(deck_spec.meta))
