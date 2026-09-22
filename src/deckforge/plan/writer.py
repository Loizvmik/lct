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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _write_one_slide(
    index: int, item, profile, prompt_body: str, source_text: str, total: int, llm: LLMProvider | None,
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

    return slide


def write_slides(
    outline: Outline, sources: list[SourceDoc], profile, llm: LLMProvider | None,
    *, max_workers: int = DEFAULT_WRITER_MAX_WORKERS,
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
            pool.submit(_write_one_slide, index, item, profile, prompt_body, source_text, total, llm): index
            for index, item in enumerate(outline.slides)
        }
        for future in as_completed(futures):
            index = futures[future]
            slides[index] = future.result()

    ordered_slides: list[SlideSpec] = slides  # type: ignore[assignment] — каждый индекс заполнен ровно один раз выше
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
