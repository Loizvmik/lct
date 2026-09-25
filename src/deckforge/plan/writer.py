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

Задача D: `rerank_patterns(deck, profile, variants, llm)` — для вариантов
`airy` и `visual` код отбирает три лучшие раскладки слайда (`variants.
rank_patterns`), модель выбирает одну по смыслу; результат уходит в
`variants.apply_variant(..., preferred=...)`. В реальном пайплайне стоит
между `write_slides` и `apply_variant` (`cli.py`, `api/jobs.py`).

Task 19: `write_slides` — теперь настоящий агентный цикл, не одиночный
вызов. Модель пишет текст вслепую, не зная, влезет ли он в слот выбранной
раскладки, — самая частая находка аудита ("текст не помещается в свою
рамку", 13 из 33 находок на контрольном прогоне брифа). `_write_with_agent_
loop` даёт модели два инструмента (`_run_tool_call`, диспетчер): `measure_
fit` — реальный замер текста в слоте раскладки (ОБЯЗАТЕЛЬНО через `compose.
textfit.measure` внутри `compose.fit_check.measure_fit` — единственный
замер текста в проекте, см. её докстроку, план по-прежнему не трогает
`Box`/координаты сам, только пересылает текст+роль и получает обратно
плоские числа) и `check_number` — водится ли число/факт в исходных
материалах слайда (`plan.factcheck.check_number_in_sources`, чистые строки,
без сети). Модель может вызвать инструменты (JSON-конверт `{"tool_calls":
[...]}`), увидеть результат и переписать текст короче — цикл, где модель
видит последствия своего действия и поправляется, а не просто "отправили,
получили ответ, код проверил". Ограничен `AGENT_MAX_STEPS_DEFAULT`
(`config/app.yaml`, `llm.slide_writer_agent_max_steps`) сетевыми кругами:
на последнем разрешённом шаге код требует финальный ответ и, если текст
всё равно не уложился, принимает его как есть — находка остаётся аудиту
(та же честная деградация "модель предлагает, код не блокирует колоду",
что и везде в проекте), а не бесконечный цикл дожимания.
"""
from __future__ import annotations
import os
import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import replace
from pathlib import Path

import yaml

from deckforge.compose.fit_check import measure_fit
from deckforge.compose.slide_tools import list_layouts, try_slide
from deckforge.plan.factcheck import check_number_in_sources
from deckforge.plan.outline import Outline, SourceDoc
from deckforge.plan.spec import (
    SLIDE_KINDS, BulletBlock, DeckSpec, SlideSpec, slide_spec_from_dict, slide_spec_problems, slide_spec_to_dict,
)
from deckforge.plan.variants import Variant, rerank_candidates
from deckforge.provider.base import LLMProvider
from deckforge.provider.yandex import WRITER_BUDGET_CAP

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

# Слайды пишутся моделью ПАРАЛЛЕЛЬНО, не по очереди — живой замер задачи
# (task-12-report.md): 23с на слайд, 12 слайдов подряд дали 273.7с из
# 302.8с всей генерации (90% времени), при том что содержание одного
# слайда не зависит от другого (свой пункт структуры, свои исходные
# материалы) — последовательный порядок был архитектурной случайностью
# первой версии `write_slides`, не требованием.
#
# Число — из `config/app.yaml` (`llm.slide_writer_max_workers`), это здесь
# только запасной дефолт, если вызывающий код не передал `max_workers` явно
# (прямые вызовы `write_slides` из тестов и т.п.) — тот же приём, что и
# `audit.visual.run_visual(..., max_workers=4)`. Значение подобрано тем же
# рассуждением, что и там: не "чем больше, тем быстрее" — провайдер (Yandex
# Cloud) не любит слишком много одновременных запросов, и в колоде и так
# уже есть запас на сетевые ретраи (`deadline_seconds`) на КАЖДЫЙ вызов;
# 4 — тот же порядок, что уже проверен живьём в визуальном аудите
# (`_PER_SLIDE_MAX_TOKENS`/`ThreadPoolExecutor(max_workers=4)`, тот же
# провайдер, тот же класс нагрузки), не гадание с нуля.
DEFAULT_WRITER_MAX_WORKERS = 4

# Task 19: сколько сетевых кругов агентного цикла разрешено ОДНОМУ слайду
# (см. докстроку модуля) — число из `config/app.yaml` (`llm.slide_writer_
# agent_max_steps`), это здесь только запасной дефолт, тот же приём, что и
# у `DEFAULT_WRITER_MAX_WORKERS` выше. 2 — буквально то, что просит бриф
# задачи ("Цикл ограничен двумя шагами. Написала, проверила, при
# необходимости переписала короче."): шаг 1 — модель пишет и, если хочет,
# зовёт инструменты (`measure_fit`/`check_number`) в том же шаге; шаг 2 —
# последний, код требует финальный ответ независимо от того, влез текст или
# нет ("не уложилась — отдаёт что есть, находка остаётся аудиту"). Слайд,
# который модель написала уверенно с первого раза (без вызова инструмента),
# по-прежнему стоит ОДИН сетевой вызов — цикл не удорожает уже хороший
# случай, только даёт модели путь исправиться в плохом.
AGENT_MAX_STEPS_DEFAULT = 2

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

# Виды слайда, где заголовок без содержания — законная вёрстка: обложка,
# разделитель раздела, героическая картинка. Тот же смысл, что у
# `patterns._HEADLINE_EXEMPT_KINDS`, но с другой стороны: там решают, нужен
# ли заголовок, здесь — нужно ли содержание под ним.
_HEADLINE_ONLY_KINDS = frozenset({"section", "image"})

# Пункт структуры (`OutlineSlide.kind`, словарь outline-writer) -> желаемый
# `SlideSpec.kind` (закрытый список `plan.spec.SLIDE_KINDS`, десять значений
# с Task 18) — грубое, но детерминированное первое приближение вёрстки,
# нужное ДО того, как известна конкретная раскладка (`pick_patterns`/
# `variants.apply_variant` идут следующими шагами): slide-writer обязан
# знать примерный лимит длины текста уже сейчас, а лимит приходит из
# вместимости раскладки ИМЕННО этого `kind` (см. `_kind_capacity`). Сам
# `kind` в ответе модели может отличаться от этой подсказки (AGENT.md
# разрешает это явно), если контент содержательно не ложится в
# предложенный тип — с Task 18 у модели для этого есть не только право, но
# и данные: `_all_kind_capacities` показывает ей вместимость ВСЕХ видов
# шаблона, не только подсказанного здесь.
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


def _all_kind_capacities(profile) -> list[dict]:
    """Вместимость ВСЕХ видов раскладки, которые реально есть в этом
    шаблоне — не только подсказанного `desired_kind` (`_kind_capacity`
    выше, та же арифметика на один вид за раз). Task 18, находка №2 брифа:
    "модель пишет прозу, потому что ей никто не сказал, что этот шаблон
    умеет показать ряд из трёх карточек... крупное число с подписью, фото
    с подписью, цитату" — до этой правки slide-writer видел вместимость
    ТОЛЬКО того вида, который код заранее выбрал `_OUTLINE_KIND_TO_SLIDE_
    KIND` (грубая эвристика по семантике пункта структуры, не по
    содержанию, которое модель ещё не написала) — увидеть, что шаблон,
    скажем, умеет цитату, было решительно неоткуда, даже когда AGENT.md уже
    прямо разрешает `kind` ответа отличаться от подсказки `layout_kind`.

    `profile is None` (синтетика/ручной вызов без шаблона) -> пустой список,
    та же честная деградация, что и `outline._summarize_available_forms`."""
    if profile is None:
        return []
    kinds_present = sorted({p.kind for p in profile.patterns})
    return [
        {
            "kind": kind,
            "count": sum(1 for p in profile.patterns if p.kind == kind),
            **_kind_capacity(kind, profile),
            "max_headline_chars": _headline_capacity(kind, profile),
        }
        for kind in kinds_present
    ]


def _fallback_slide(kind: str, index: int, intent: str, needs: list[str], *, reason: str | None = None) -> SlideSpec:
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
    if reason:
        finding += f" Причина: {reason}."
    if needs:
        finding += f" Нужны данные: {', '.join(needs)}."
    source_note = "Источник не подтверждён — текст запасного варианта, требует проверки перед показом." \
        if _DIGIT_RE.search(headline) else None

    # Слайд с ОДНИМ заголовком — брак по ТЗ (Приложение 1, «Целостность»:
    # «пустой слайд или слайд с одним заголовком»). Раньше запасной вариант
    # выдавал ровно его: на живом прогоне 25 сентября 2026 три слайда из
    # двенадцати вышли пустыми, и по слайду было не понять, что случилось.
    #
    # Пункты структуры (`OutlineSlide.needs`) — это то, что планировщик
    # просил найти в источниках для ЭТОГО слайда. Показать их честнее, чем
    # пустоту: видно, чего не хватило, и слайд можно дописать руками, не
    # разбираясь в отчёте.
    # На обложке и разделителе слайд с одним заголовком — норма вёрстки, а
    # не брак: ТЗ имеет в виду содержательный слайд, на котором кроме
    # заголовка ничего нет. Живой прогон 25 сентября 2026 на VK Tech:
    # титульный слайд получил строку «Нужны данные: Название инициативы» —
    # для обложки это мусор, а не честность.
    needs_fit_this_slide = needs and kind not in _HEADLINE_ONLY_KINDS
    blocks = [BulletBlock(items=[f"Нужны данные: {n}" for n in needs])] if needs_fit_this_slide else []
    return SlideSpec(
        index=index, kind=kind, headline=headline, blocks=blocks,
        source_note=source_note, findings=[finding],
    )


_SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(SLIDE_KINDS)},
        "layout_id": {"type": "string"},
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


# Task 19 — конверт вызова инструмента, которым модель может ответить
# ВМЕСТО финального слайда на любом шаге, кроме последнего (см. `_AGENT_
# TURN_SCHEMA`/`_write_with_agent_loop`). Список, не одиночный вызов —
# модель может захотеть проверить и заголовок, и цифру в одном шаге, а
# бюджет сетевых кругов (`AGENT_MAX_STEPS_DEFAULT`) считает именно КРУГИ,
# не отдельные вызовы инструментов внутри круга (оба инструмента —
# локальные вычисления, не сеть, батч из нескольких вызовов в одном шаге
# ничего не стоит по времени сверх самого шага).
_TOOL_CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": ["measure_fit", "check_number", "list_layouts", "try_slide"]},
                    "args": {"type": "object"},
                },
                "required": ["tool", "args"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tool_calls"],
    "additionalProperties": False,
}

# Схема НЕ последнего шага цикла: модель вправе ответить либо вызовом
# инструмента, либо сразу финальным слайдом (если уверена без проверки) —
# `oneOf`, не приоритет одного варианта над другим (докстрока `provider.
# yandex.YandexProvider.complete`: `schema` здесь — только текстовая
# подсказка модели в system-сообщении, не строгий JSON Schema-режим API,
# так что `oneOf` читает модель, не валидатор).
_AGENT_TURN_SCHEMA = {"oneOf": [_TOOL_CALL_SCHEMA, _SLIDE_SCHEMA]}


def _run_tool_call(
    call: dict, profile, desired_kind: str, source_text: str, index: int = 0,
    template_path: Path | None = None,
) -> dict:
    """Диспетчер инструментов агентного цикла (см. докстроку модуля).
    Никогда не бросает исключение наружу — невалидный/неизвестный вызов
    (модель перепутала имя инструмента или прислала не те аргументы)
    возвращает объект с `error`, который уходит обратно модели тем же
    путём, что и настоящий результат: она видит свою ошибку и может
    попробовать снова на следующем шаге, вместо того чтобы уронить весь
    цикл написания этого слайда."""
    if not isinstance(call, dict):
        return {"error": "вызов инструмента должен быть объектом {tool, args}"}
    name = call.get("tool")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    try:
        if name == "measure_fit":
            return measure_fit(str(args.get("text", "")), str(args.get("role", "")), profile, desired_kind)
        if name == "check_number":
            return check_number_in_sources(str(args.get("query", "")), source_text)
        # Оба инструмента ниже (Task 23) требуют файла шаблона: черновик
        # рисуется от него же, что и настоящая колода. Без пути они просто
        # недоступны — так работает вызов `write_slides` из тестов и из
        # старого кода, который путь не передаёт: цикл продолжается на
        # прежних двух инструментах, а не падает.
        if name == "list_layouts":
            if template_path is None:
                return {"error": "инструмент недоступен в этом прогоне (шаблон не передан)"}
            kind = args.get("kind")
            return {"layouts": list_layouts(profile, kind=str(kind) if kind else None)}
        if name == "try_slide":
            if template_path is None:
                return {"error": "инструмент недоступен в этом прогоне (шаблон не передан)"}
            return _try_slide_tool(args, profile, index, template_path)
    except Exception as exc:  # инструмент не должен ронять весь цикл написания слайда
        return {"error": f"инструмент {name!r} упал: {exc}"}
    return {
        "error": f"неизвестный инструмент {name!r}, доступны: "
                 "measure_fit, check_number, list_layouts, try_slide"
    }


def _try_slide_tool(args: dict, profile, index: int, template_path: Path) -> dict:
    """Черновая сборка слайда по ответу модели — обёртка над `compose.
    slide_tools.try_slide`, разбирающая аргументы так же снисходительно, как
    и остальные инструменты: модель прислала слайд не по схеме или забыла
    номер раскладки — это ответ ей текстом, а не исключение."""
    layout_id = args.get("layout_id")
    if not layout_id:
        return {"error": "нужен layout_id — возьми его из list_layouts"}
    draft = args.get("slide")
    if not isinstance(draft, dict):
        return {"error": "нужен объект slide — тот же JSON слайда, что ты собираешься прислать финальным"}
    try:
        spec = slide_spec_from_dict(draft, index)
    except Exception as exc:
        return {"error": f"слайд не лёг в схему: {exc}"}
    return try_slide(spec, str(layout_id), profile, template_path)


def _complete(llm: LLMProvider, messages: list[dict], schema: dict, max_tokens: int) -> str:
    """Вызов модели с потолком бюджета, поднятым для писателя слайдов.

    `budget_cap` понимает не всякий провайдер (у базового интерфейса его
    нет, тесты подсовывают свои заглушки) — поэтому сначала пробуем с ним,
    а на `TypeError` зовём по-старому. Это не проглатывание ошибки: любая
    другая беда провайдера летит наверх и попадает в причину отказа."""
    try:
        return llm.complete(messages, schema=schema, max_tokens=max_tokens, budget_cap=WRITER_BUDGET_CAP)
    except TypeError:
        return llm.complete(messages, schema=schema, max_tokens=max_tokens)


def _why(exc: BaseException) -> str:
    """Короткая причина отказа для отчёта — класс исключения плюс начало
    сообщения.

    Ключ провайдера вырезается: он уходит в заголовок запроса, а не в текст
    исключения, но причина попадает в `SlideSpec.findings`, оттуда в отчёт
    прогона и в веб-интерфейс, и цена ошибки тут несимметрична — лучше
    вырезать лишнее, чем однажды показать секрет на экране."""
    text = f"{type(exc).__name__}: {str(exc)[:200]}"
    for name in ("YANDEX_API_KEY", "YANDEX_FOLDER_ID"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "***")
    return text


def _write_with_agent_loop(
    prompt_body: str, payload: dict, index: int, llm: LLMProvider, profile, desired_kind: str,
    source_text: str, *, max_steps: int, template_path: Path | None = None,
) -> tuple[SlideSpec | None, str | None]:
    """Task 19: агентный цикл письма ОДНОГО слайда, бюджет `max_steps`
    сетевых кругов (см. `AGENT_MAX_STEPS_DEFAULT`). Заменяет первый вызов
    `_ask_slide_writer` в `_write_one_slide` — последующий один шанс
    исправить СТРУКТУРНО невалидный ответ (`slide_spec_problems`) остаётся
    снаружи, в `_write_one_slide`, без изменений: это разные заботы (там —
    "ответ вообще разбирается по схеме", здесь — "текст физически влезает и
    цифры не выдуманы").

    На каждом шаге, кроме последнего, модель вольна ответить вызовом
    инструмента (`{"tool_calls": [...]}`) вместо финального слайда — код
    выполняет все вызовы этого шага, добавляет их результаты отдельным
    user-сообщением и переходит к следующему шагу. Любой другой валидный
    JSON-объект (без `tool_calls`) трактуется как попытка финального
    ответа — цикл завершается ЭТИМ шагом, даже если `max_steps` ещё не
    исчерпан: слайд, написанный уверенно с первого раза, не должен стоить
    больше одного сетевого вызова.

    На последнем разрешённом шаге инструменты уже недоступны (`is_final_
    step`) — код прямо просит модель ответить финальным слайдом, и ЛЮБОЙ
    ответ на этом шаге, включая случайный `tool_calls`, парсится как
    попытка слайда (и, скорее всего, провалится валидацией схемы —
    `slide_spec_from_dict` подберёт это как обычную ошибку разбора, тот же
    путь, что и раньше у любого невалидного ответа).

    Возвращает `(слайд, причина_отказа)`. Раньше возвращала только слайд, а
    причину глотала (`except Exception: return None`) — и запасной слайд
    получал одну и ту же строку «модель недоступна или не вернула валидный
    ответ» независимо от того, отвалилась сеть, кончился бюджет токенов или
    ответ не разобрался по схеме. На живом прогоне 23 сентября 2026 четыре
    слайда из двенадцати ушли в запасной вариант, и понять почему было
    нечем."""
    conversation: list[dict] = []
    for step in range(1, max(1, max_steps) + 1):
        is_final_step = step >= max_steps
        turn_payload = dict(payload)
        if is_final_step and max_steps > 1:
            turn_payload["_agent_step"] = "final — ответь ТОЛЬКО финальным JSON слайда, вызовы инструментов больше недоступны"
        messages = [
            {"role": "system", "content": prompt_body},
            {"role": "user", "content": json.dumps(turn_payload, ensure_ascii=False)},
            *conversation,
        ]
        schema = _SLIDE_SCHEMA if is_final_step else _AGENT_TURN_SCHEMA
        try:
            raw = _complete(llm, messages, schema, WRITER_MAX_TOKENS)
        except Exception as exc:
            return None, f"шаг {step}/{max_steps}: модель не ответила — {_why(exc)}"
        try:
            data = json.loads(raw)
        except Exception as exc:
            return None, f"шаг {step}/{max_steps}: ответ не разобрался как JSON — {_why(exc)}"
        if not isinstance(data, dict):
            return None, f"шаг {step}/{max_steps}: модель вернула {type(data).__name__}, а не объект JSON"

        tool_calls = data.get("tool_calls")
        if not is_final_step and isinstance(tool_calls, list) and tool_calls:
            results = [
                {"tool": call.get("tool") if isinstance(call, dict) else None,
                 "args": call.get("args") if isinstance(call, dict) else None,
                 "result": _run_tool_call(
                     call, profile, desired_kind, source_text, index, template_path)}
                for call in tool_calls
            ]
            conversation.append({"role": "assistant", "content": raw})
            conversation.append(
                {"role": "user", "content": json.dumps({"tool_results": results}, ensure_ascii=False)}
            )
            continue

        try:
            slide = slide_spec_from_dict(data, index)
        except Exception as exc:
            return None, f"шаг {step}/{max_steps}: ответ не лёг в схему слайда — {_why(exc)}"

        return slide, None
    return None, f"бюджет шагов исчерпан ({max_steps}), финального слайда модель так и не прислала"


def _ask_slide_writer(
    prompt_body: str, payload: dict, index: int, llm: LLMProvider, *, repair: list[str] | None = None,
) -> tuple[SlideSpec | None, str | None]:
    """Один вызов модели за слайдом. Возвращает `(слайд, причина_отказа)` —
    тем же контрактом, что и `_write_with_agent_loop` выше и по той же
    причине."""
    user_payload = dict(payload)
    if repair:
        user_payload["previous_answer_problems"] = repair
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    try:
        raw = _complete(llm, messages, _SLIDE_SCHEMA, WRITER_MAX_TOKENS)
    except Exception as exc:
        return None, f"модель не ответила — {_why(exc)}"
    try:
        slide = slide_spec_from_dict(json.loads(raw), index)
    except Exception as exc:
        return None, f"ответ не лёг в схему слайда — {_why(exc)}"
    return slide, None


def _write_one_slide(
    index: int, item, profile, prompt_body: str, source_text: str, total: int, llm: LLMProvider | None,
    *, agent_max_steps: int = AGENT_MAX_STEPS_DEFAULT, template_path: Path | None = None,
) -> SlideSpec:
    """Пишет ОДИН слайд — вынесено из `write_slides` в отдельную функцию,
    чтобы её можно было независимо запускать в пуле потоков (слайды друг от
    друга не зависят: свой пункт структуры, свои исходные материалы — тот
    же аргумент, что уже обосновал параллельность `audit.visual.run_visual`
    по слайдам). Не трогает ничего снаружи себя (не пишет в общий список,
    не читает состояние других слайдов) — единственное, что нужно для
    безопасного вызова из нескольких потоков одновременно."""
    desired_kind = _OUTLINE_KIND_TO_SLIDE_KIND.get(item.kind, "bullets")
    capacity = _kind_capacity(desired_kind, profile)
    payload = {
        "slide_kind_hint": item.kind,
        "intent": item.intent,
        "needs": item.needs,
        "layout_kind": desired_kind,
        "capacity": capacity,
        "max_headline_chars": _headline_capacity(desired_kind, profile),
        # Task 18: ВСЕ виды раскладки, которые реально есть в шаблоне, с их
        # вместимостью — не только подсказанный `layout_kind` (см.
        # `_all_kind_capacities`) — модель решает, что из материала ложится
        # в цитату/крупный фактоид/карточки/фото, а не только в прозу.
        "available_kinds": _all_kind_capacities(profile),
        "sources": source_text,
        "position": {"index": index, "total": total},
    }

    slide: SlideSpec | None = None
    reason: str | None = None
    if llm is not None:
        # Task 19: первый шанс — агентный цикл (пишет, при необходимости
        # меряет текст/сверяет цифры инструментами, переписывает), не
        # одиночный вызов. Репарация СТРУКТУРНОЙ невалидности ниже — та же,
        # что была всегда, отдельная забота (см. докстроку `_write_with_
        # agent_loop`).
        slide, reason = _write_with_agent_loop(
            prompt_body, payload, index, llm, profile, desired_kind, source_text,
            max_steps=agent_max_steps, template_path=template_path,
        )
        if slide is not None:
            problems = slide_spec_problems(slide)
            if problems:
                # Один шанс на исправление — код показывает модели её
                # собственные ошибки (брифом: "валидатор ловит то, что
                # иначе всплывёт при сборке" — здесь оно ловится ДО
                # сборки и ДО того, как испортит остальную колоду).
                repaired, repair_reason = _ask_slide_writer(prompt_body, payload, index, llm, repair=problems)
                still_broken = slide_spec_problems(repaired) if repaired is not None else []
                if repaired is not None and not still_broken:
                    slide = repaired
                else:
                    slide = None
                    reason = (
                        f"ответ не прошёл проверку ({'; '.join(problems)}), "
                        + (
                            f"попытка исправить тоже: {'; '.join(still_broken)}"
                            if still_broken else f"попытка исправить: {repair_reason}"
                        )
                    )
    elif llm is None:
        reason = "модель не подключена (нет ключа) — текст слайдов не писался вовсе"

    if slide is None:
        slide = _fallback_slide(desired_kind, index, item.intent, item.needs, reason=reason)

    return _validate_chosen_layout(slide, profile)


def _validate_chosen_layout(slide: SlideSpec, profile) -> SlideSpec:
    """Номер раскладки, выбранный агентом, обязан существовать в ЭТОМ
    шаблоне — тот же принцип «модель предлагает, код проверяет», что и у
    именования палитры и выбора вида раскладки.

    Живой прогон 23 сентября 2026: модель прислала `layout_id: "bullets_1"`,
    хотя раскладки этого шаблона называются `slide16`/`slide18` — то есть
    выдумала номер, не посмотрев каталог. `compose.builder._resolve_pattern`
    такой номер и так не примет (он ищет паттерн по id и не находит), но
    молча: со стороны это неотличимо от «агент не выбирал». Выдуманный
    номер стирается здесь и остаётся честной находкой."""
    if not slide.pattern_id:
        return slide
    known = {p.pattern_id for p in profile.patterns}
    if slide.pattern_id in known:
        return slide
    slide.findings.append(
        f"Слайд {slide.index}: раскладки {slide.pattern_id!r} в шаблоне нет — "
        "выбор модели отброшен, раскладку подобрал код. Номер надо брать из list_layouts."
    )
    return replace(slide, pattern_id=None)


def write_slides(
    outline: Outline, sources: list[SourceDoc], profile, llm: LLMProvider | None,
    *, max_workers: int = DEFAULT_WRITER_MAX_WORKERS, agent_max_steps: int = AGENT_MAX_STEPS_DEFAULT,
    template_path: Path | None = None,
) -> DeckSpec:
    """Пишет текст всех слайдов ПАРАЛЛЕЛЬНО (см. `DEFAULT_WRITER_MAX_
    WORKERS` — до `max_workers` одновременных вызовов модели), не по
    очереди — слайды друг от друга не зависят. Два инварианта, за которые
    отвечает именно эта функция (а не `_write_one_slide`, который ничего не
    знает про порядок и про соседей):

    1. Порядок слайдов в готовой колоде не зависит от того, кто ответил
       первым — результаты собираются в массив по ИНДЕКСУ (`slides[index]
       = ...`), не в порядке `as_completed`, и уже упорядоченный список
       уходит дальше (`_flag_repeated_headlines`, `DeckSpec.slides`).
    2. Отказ одного слайда (исключение/невалидный ответ внутри
       `_write_one_slide`) не роняет всю колоду — `_write_one_slide` сама
       никогда не бросает исключение по вине модели (та же деградация, что
       и раньше, до параллельности: `_ask_slide_writer` ловит любую ошибку
       вызова и возвращает `None`, дальше в ход идёт `_fallback_slide`);
       здесь это свойство только ПЕРЕЖИВАЕТ переезд в пул потоков, не
       создаётся заново."""
    _meta, prompt_body = _load_agent_prompt(AGENT_PATH_WRITER)
    source_text = "\n\n".join(f"### {s.name}\n{s.text}" for s in sources)
    total = len(outline.slides)

    slides: list[SlideSpec | None] = [None] * total
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(
                _write_one_slide, index, item, profile, prompt_body, source_text, total, llm,
                agent_max_steps=agent_max_steps, template_path=template_path,
            ): index
            for index, item in enumerate(outline.slides)
        }
        for future in as_completed(futures):
            index = futures[future]
            slides[index] = future.result()

    ordered_slides: list[SlideSpec] = slides  # type: ignore[assignment] — каждый индекс заполнен ровно один раз выше
    ordered_slides = _drop_thin_duplicates(ordered_slides)
    _flag_repeated_headlines(ordered_slides)
    return DeckSpec(title=outline.title, language=outline.language, slides=ordered_slides)


# Слово короче этой длины (предлоги, союзы, частицы — «и», «на», «за») не
# несёт содержательного веса и не должно засчитываться как «общий факт»
# двух заголовков — русские короткие служебные слова совпадают у ЛЮБОЙ пары
# предложений на одну тему, не только у настоящих повторов.
_SIGNIFICANT_WORD_MIN_LEN = 4

# Доля значимых слов ОДНОГО (более короткого) заголовка, которая обязана
# совпасть с другим, чтобы пара считалась "тем же фактом другими словами" —
# найдено обязательной проверкой задачи (координатор поймал на живом
# рендере: "98,5% времени заявки ждут, а не обрабатываются" и "98,5%
# времени заявка находится в ожидании, а не в активной обработке" — разное
# падежное окончание и порядок слов, но 5 из 6 значимых слов короче
# совпадают буквально). 0.5 — заметно выше случайного совпадения (заголовки
# одной колоды неизбежно делят домен-специфичные слова вроде "маршрутизация"
# просто по теме презентации), но ниже почти дословного повтора. Findings —
# информационные, не блокирующие сборку: цена ложного срабатывания (лишняя
# строка в отчёте) ниже цены пропуска настоящего повтора, порог — по
# нижней границе "заметно выше случайного", не по верхней.
_HEADLINE_OVERLAP_THRESHOLD = 0.5


def _significant_words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]+", text) if len(w) >= _SIGNIFICANT_WORD_MIN_LEN}


# Число в заголовке ("98,5%", "6,2 ч", "80%") — сильный, самостоятельный
# признак того же факта: находка ручной проверки задачи (координатор,
# ЛЦТ2026) — "98,5% ожидания устранимы: пилот подтвердил эффективность,
# готов план раскатки" и "98,5% времени заявка находится в ожидании, а не
# обрабатывается" несут один и тот же факт, но делят НОЛЬ значимых слов
# буквально (падеж "ожидания"/"ожидании" не совпадает посимвольно, порядок
# и состав фразы разный) — чистое сравнение слов (см. `_significant_words`)
# эту пару пропускало НЕЗАВИСИМО от того, соседние слайды или нет (сам
# бэкстоп уже сравнивал ВСЮ колоду, а не только соседей, — не в этом была
# проблема). Матчатся только "заметные" числа — с десятичным разделителем
# и/или процентом (типичная форма метрики в этом проекте: "98,5%", "31,5",
# "80%"), НЕ голые целые ("2", "2026", "4 подразделения") — те слишком
# часто случайно совпадают у НЕСВЯЗАННЫХ фактов одной колоды (год, счётчик,
# порядковый номер) и дали бы шумные ложные находки.
_NOTABLE_NUMBER_RE = re.compile(r"\d+[.,]\d+%?|\d+%")


def _notable_numbers(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("%") for n in _NOTABLE_NUMBER_RE.findall(text)}


# Колода короче стольких слайдов дублей не теряет: там повтор заметен и
# глазом, а выбрасывание сделало бы её куцей.
_MIN_SLIDES_TO_DEDUP = 6

# «Тонкий» слайд: столько или меньше единиц содержания (пунктов, карточек,
# метрик, абзацев) и без таблицы/графика. Такой слайд, повторяющий факт
# соседа, не добавляет ничего, кроме самого повтора.
_THIN_SLIDE_MAX_UNITS = 1


def _content_units(slide: SlideSpec) -> int:
    units = 0
    for block in slide.blocks:
        items = getattr(block, "items", None)
        units += len(items) if items is not None else 1
    if slide.visual is not None and slide.visual.kind in ("table", "chart"):
        units += 3
    return units


def _same_fact(a: SlideSpec, b: SlideSpec) -> bool:
    words_a, words_b = _significant_words(a.headline), _significant_words(b.headline)
    overlap = len(words_a & words_b) / min(len(words_a), len(words_b)) if words_a and words_b else 0.0
    return overlap >= _HEADLINE_OVERLAP_THRESHOLD or bool(_notable_numbers(a.headline) & _notable_numbers(b.headline))


def _drop_thin_duplicates(slides: list[SlideSpec]) -> list[SlideSpec]:
    """Убирает слайд, который повторяет факт другого слайда (тот же признак,
    что у `_flag_repeated_headlines`) и при этом тонкий (`_content_units`
    не больше `_THIN_SLIDE_MAX_UNITS`). Прогон 26 сентября 2026 на VK
    Education: слайды 3, 4 и 5 все про «98,5% времени ожидание», два из них
    с одним пунктом; из двенадцати слайдов уникальных восемь. Находка
    `_flag_repeated_headlines` это видела, но ничего не меняла.

    Из пары выбрасывается тонкий; если тонкие оба, тот, что позже.
    Обложка (первый) и финал (последний) не трогаются: у них по одному
    заголовку по замыслу. Индексы оставшихся пересчитываются подряд, чтобы
    находки и сборка ссылались на итоговые номера."""
    if len(slides) < _MIN_SLIDES_TO_DEDUP:
        return slides
    dropped: set[int] = set()
    for i in range(len(slides)):
        for j in range(i + 1, len(slides)):
            if i in dropped or j in dropped or not _same_fact(slides[i], slides[j]):
                continue
            protected = {0, len(slides) - 1}
            thin_i = _content_units(slides[i]) <= _THIN_SLIDE_MAX_UNITS and i not in protected
            thin_j = _content_units(slides[j]) <= _THIN_SLIDE_MAX_UNITS and j not in protected
            if thin_j:
                dropped.add(j)
            elif thin_i:
                dropped.add(i)
    if not dropped:
        return slides
    kept = [s for k, s in enumerate(slides) if k not in dropped]
    return [replace(s, index=k) for k, s in enumerate(kept)]


def _flag_repeated_headlines(slides: list[SlideSpec]) -> None:
    """Помечает finding'ом пары слайдов КОЛОДЫ ЦЕЛИКОМ (не только соседних —
    двойной цикл `i < j` ниже сравнивает КАЖДУЮ пару, брифом задачи прямо
    требуется не ограничиваться соседями), чьи заголовки, вероятно, несут
    один и тот же главный факт другими словами — код-бэкстоп поверх правила
    `agents/outline-writer/AGENT.md` ("не повторяй тот же факт в двух
    соседних пунктах"): промпт не гарантирует соблюдение (находка
    обязательной проверки задачи — координатор поймал повтор на живом
    рендере контрольного шаблона), а `audit.deterministic` I06 ловит только
    ПОЧТИ ДОСЛОВНЫЙ повтор (`duplicate_similarity=0.9` посимвольно, см.
    `config/audit.yaml`) — перефразированный повтор той же цифры/вывода
    другими словами через него не проходит.

    Два независимых сигнала совпадения, любой сам по себе достаточен (доля
    общих значимых слов ИЛИ общее заметное число, см. `_notable_numbers`):
    сравнение "по голому совпадению строк" не ловит перефразировку с другим
    падежом/порядком слов (находка задачи — доля общих слов была 0.0 у
    настоящего повтора), а числа в заголовке — самостоятельно сильный
    признак того же факта (бриф задачи: "одна и та же цифра в двух
    заголовках почти всегда означает повтор") независимо от того,
    пересеклись ли слова вокруг неё. Не переписывает и не объединяет
    слайды (для этого нужен ещё один вызов модели, вне бюджета времени
    колоды) — только честно помечает находкой, как и любое другое
    расхождение содержания с ожиданием в этом проекте."""
    word_sets = [_significant_words(s.headline) for s in slides]
    number_sets = [_notable_numbers(s.headline) for s in slides]
    for i in range(len(slides)):
        for j in range(i + 1, len(slides)):
            a, b = word_sets[i], word_sets[j]
            word_overlap = len(a & b) / min(len(a), len(b)) if a and b else 0.0
            shared_numbers = number_sets[i] & number_sets[j]
            if word_overlap >= _HEADLINE_OVERLAP_THRESHOLD or shared_numbers:
                slides[i].findings.append(
                    f"Слайд {slides[i].index}: заголовок похож на слайд {slides[j].index} "
                    f"({slides[j].headline!r}) — возможно, один и тот же факт другими словами."
                )


# ---------------------------------------------------------------------------
# pick_patterns — подбор КОНКРЕТНОЙ раскладки моделью, поверх уже
# написанного содержания.
# ---------------------------------------------------------------------------

_PICKER_SCHEMA = {
    "type": "object",
    "properties": {"pattern_id": {"type": "string"}, "reason": {"type": "string"}},
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


# ---------------------------------------------------------------------------
# rerank_patterns — модель выбирает раскладку из трёх, отобранных кодом,
# для вариантов airy и visual.
# ---------------------------------------------------------------------------

# Варианты, которым раскладку уточняет модель. `dense` сюда не входит: его
# раскладку уже выбирал писатель текста инструментами (`list_layouts`/
# `try_slide`), и `apply_variant` её наследует.
RERANK_VARIANTS = (Variant.visual, Variant.airy)

# Запасные дефолты, если вызывающий код не передал своих: те же числа, что
# в `config/app.yaml` (`llm.pattern_picker_*`), см. обоснование там.
DEFAULT_RERANK_MAX_WORKERS = 8
DEFAULT_RERANK_BUDGET_SECONDS = 40.0

# Стартовый бюджет ответа одного вызова. Живой прогон 26 сентября 2026 на
# VK Education: с прежних 2048 (`PICKER_MAX_TOKENS`) провайдер эскалировал до
# 4096 в 11 вызовах из 14 — модель рассуждает, прежде чем выбрать, и почти
# каждый вызов платил лишний сетевой круг. Начинать сразу с 4096 дешевле.
RERANK_MAX_TOKENS = 4096

# Поля слайда, которые модели не нужны для выбора раскладки: находки и
# заметки докладчика её только отвлекают, а `pattern_id` — это выбор
# писателя для плотного варианта, не подсказка для этих двух.
_RERANK_HIDDEN_SLIDE_FIELDS = ("findings", "speaker_notes", "pattern_id", "index")


def _rerank_candidate_card(pattern, rank: int) -> dict:
    """Что модель знает о раскладке: роли и вместимость мест, сетка, декор.
    Без координат: план их не знает (см. `tests/plan/test_no_pptx_import.
    py`), а смысл выбора координатами и не выражается."""
    return {
        "pattern_id": pattern.pattern_id,
        "rank": rank,
        "kind": pattern.kind,
        "slots": [{"role": slot.role, "max_chars": slot.max_chars} for slot in pattern.slots],
        "repeat_count": pattern.repeat.count if pattern.repeat is not None else 0,
        "decor_count": len(pattern.decor),
        "has_image_slot": any(slot.role in ("image", "icon") for slot in pattern.slots),
        "is_dark": pattern.is_dark,
    }


def _rerank_one(
    variant: Variant, slide: SlideSpec, candidate_ids: list[str], profile, prompt_body: str, llm: LLMProvider,
) -> tuple[str, str | None]:
    """Один вызов модели на слайд. Возвращает `(pattern_id, причина сбоя)`:
    при любом сбое — первого кандидата кода и причину, иначе `None`. Не
    бросает: отказ модели на одном слайде не должен ронять шаг. Пояснение
    модели (`reason`) нужно ей самой, чтобы выбирать осмысленно, коду оно
    ни к чему."""
    by_id = {p.pattern_id: p for p in profile.patterns}
    slide_view = {k: v for k, v in slide_spec_to_dict(slide).items() if k not in _RERANK_HIDDEN_SLIDE_FIELDS}
    payload = {
        "variant": variant.value,
        "slide": slide_view,
        "candidates": [_rerank_candidate_card(by_id[pid], rank) for rank, pid in enumerate(candidate_ids, 1)],
    }
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    fallback = candidate_ids[0]
    try:
        raw = llm.complete(messages, schema=_PICKER_SCHEMA, max_tokens=RERANK_MAX_TOKENS)
        data = json.loads(raw)
    except Exception as exc:
        return fallback, f"модель не ответила — {_why(exc)}"
    proposed = data.get("pattern_id") if isinstance(data, dict) else None
    if not isinstance(proposed, str) or proposed not in candidate_ids:
        # Тот же принцип, что `_validate_chosen_layout`: номер вне списка
        # кандидатов код не принимает.
        return fallback, f"модель назвала раскладку вне списка кандидатов ({proposed!r})"
    return proposed, None


def rerank_patterns(
    deck: DeckSpec, profile, variants, llm: LLMProvider | None,
    *, max_workers: int = DEFAULT_RERANK_MAX_WORKERS, budget_seconds: float = DEFAULT_RERANK_BUDGET_SECONDS,
    notes: list[str] | None = None,
) -> dict[tuple[Variant, int], str]:
    """Раскладки, выбранные моделью: `(вариант, номер слайда в варианте) ->
    pattern_id`, готово к передаче в `apply_variant(..., preferred=...)`.

    Зачем: для `airy` и `visual` выбор раньше был чисто числовым (`variants.
    _pattern_rank_key`), без единого взгляда на смысл слайда, и два варианта
    из трёх выглядели однообразно. Код по-прежнему решает, что вообще
    допустимо (`variants.rank_patterns`: три лучших, все без переполнения),
    модель выбирает одну по смыслу. `apply_variant` остаётся чистой и ещё
    раз проверяет совместимость.

    Время: вызовы параллельны (`max_workers`), а весь шаг ограничен
    `budget_seconds`. Слайды, по которым модель не успела, просто остаются
    на выборе кода: колода и так близка к 300с ТЗ, и ждать отстающих ради
    вкуса дороже, чем их потерять. Без модели шаг ничего не делает.

    `notes` (если передан) получает по строке на каждый слайд, где модель
    не ответила или ответила не по правилам, — для отчёта командной строки."""
    if llm is None:
        return {}
    wanted = [v for v in RERANK_VARIANTS if v in set(variants)]
    jobs = [
        (variant, index, slide, ids)
        for variant in wanted
        for index, slide, ids in rerank_candidates(deck, profile, variant)
    ]
    if not jobs:
        return {}
    _meta, prompt_body = _load_agent_prompt(AGENT_PATH_PICKER)

    result: dict[tuple[Variant, int], str] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
    try:
        futures = {
            pool.submit(_rerank_one, variant, slide, ids, profile, prompt_body, llm): (variant, index, ids)
            for variant, index, slide, ids in jobs
        }
        deadline = time.monotonic() + budget_seconds
        pending = set(futures)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                variant, index, _ids = futures[future]
                chosen, failure = future.result()
                result[(variant, index)] = chosen
                if notes is not None and failure:
                    notes.append(f"[{variant.value}] слайд {index}: {failure}, взят первый кандидат кода")
        for future in pending:
            variant, index, ids = futures[future]
            result[(variant, index)] = ids[0]
            if notes is not None:
                notes.append(
                    f"[{variant.value}] слайд {index}: модель не уложилась в бюджет шага "
                    f"({budget_seconds:.0f}с), взят первый кандидат кода"
                )
    finally:
        # Не ждать отстающих: их вызовы сами оборвутся по дедлайну провайдера,
        # а ответ уже никому не нужен.
        pool.shutdown(wait=False, cancel_futures=True)
    return result
