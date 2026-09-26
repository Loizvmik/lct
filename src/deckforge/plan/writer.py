"""Пишет текст слайдов под контракт уже выбранной раскладки (раздел 10).

Порядок решений с задачи P: структура (`outline.build_outline`) ->
раскладка на каждый слайд сразу для всей колоды (`pattern.plan_patterns`,
по стилю) -> контракт слайда (`plan.contracts`) -> текст. Писатель больше
не выбирает раскладку и не видит каталог раскладок: он получает контракт
(заголовок до N слов, K карточек по M слов, таблица до R строк) и пишет
под него. Раньше было наоборот: текст писался под «самую вместительную
раскладку вида», раскладка подбиралась после, и нарядные раскладки с
местами на 36-84 знака отклонялись, потому что текст был на 140-200.

Один вызов модели на слайд, слайды параллельно (до `max_workers`),
агентный цикл до `agent_max_steps` кругов с двумя инструментами:
`measure_fit` (влезет ли текст в место раскладки, реальные метрики шрифта)
и `check_number` (есть ли число в исходниках). Ответ проверяется кодом
дважды: по схеме слайда (`spec.slide_spec_problems`) и по контракту
(`contracts.contract_problems`: число единиц, пределы слов и знаков). При
нарушении один ремонтный вызов «сократи/дополни под контракт»; из двух
ответов берётся тот, что ближе к контракту. Модель недоступна или дважды
не справилась: запасной слайд из пунктов плана, с честной находкой.

Вид слайда и раскладку ответ модели не меняет: `kind` и `pattern_id`
приходят из контракта."""
from __future__ import annotations
import os
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

import yaml

from deckforge.compose.fit_check import measure_fit
from deckforge.pattern.planner import repick_pattern
from deckforge.plan.contracts import SlideContract, contract_fill, contract_problems
from deckforge.plan.factcheck import check_number_in_sources
from deckforge.plan.normalize import normalize_deck
from deckforge.plan.outline import Outline, SourceDoc
from deckforge.plan.spec import (
    SLIDE_KINDS, BulletBlock, Card, CardBlock, DeckSpec, SlideSpec, TextBlock, Visual,
    slide_spec_from_dict, slide_spec_problems, slide_spec_to_dict,
)
from deckforge.provider.base import LLMProvider
from deckforge.provider.yandex import WRITER_BUDGET_CAP

AGENT_PATH_WRITER = Path(__file__).resolve().parents[3] / "agents" / "slide-writer" / "AGENT.md"

# Живой прогон: на старте 3072 эскалация до потолка бюджета токенов
# срабатывала почти на каждом вызове писателя; начинать ниже нужного
# значит терять HTTP-круг.
WRITER_MAX_TOKENS = 6144

# Слайды пишутся параллельно: содержание одного не зависит от другого.
# Число из `config/app.yaml` (`llm.slide_writer_max_workers`), здесь только
# запасной дефолт для прямых вызовов.
DEFAULT_WRITER_MAX_WORKERS = 4

# Сетевых кругов агентного цикла на слайд (`llm.slide_writer_agent_max_steps`):
# шаг 1 пишет и, если хочет, зовёт инструменты; шаг 2 последний, код
# требует финальный ответ.
AGENT_MAX_STEPS_DEFAULT = 2

_DIGIT_RE = re.compile(r"\d")


def _load_agent_prompt(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path}: ожидался YAML-фронтматтер, ограниченный `---`")
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


# Поля ответа модели. `kind` и `layout_id` больше не её решение, но старый
# ответ с ними не должен падать: код их перезаписывает из контракта.
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
    "required": ["headline"],
    "additionalProperties": False,
}

_TOOL_NAMES = ("measure_fit", "check_number")

_TOOL_CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": list(_TOOL_NAMES)},
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

_AGENT_TURN_SCHEMA = {"oneOf": [_TOOL_CALL_SCHEMA, _SLIDE_SCHEMA]}


def _run_tool_call(call: dict, profile, contract: SlideContract, source_text: str) -> dict:
    """Диспетчер инструментов. Не бросает: невалидный вызов возвращает
    `error`, модель видит его и может исправиться на следующем шаге."""
    if not isinstance(call, dict):
        return {"error": "вызов инструмента должен быть объектом {tool, args}"}
    name = call.get("tool")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    try:
        if name == "measure_fit":
            # Место меряется в раскладке этого слайда: она уже выбрана.
            return measure_fit(
                str(args.get("text", "")), str(args.get("role", "")), profile, contract.kind,
                layout_id=contract.pattern_id,
            )
        if name == "check_number":
            return check_number_in_sources(str(args.get("query", "")), source_text)
    except Exception as exc:  # инструмент не должен ронять цикл написания слайда
        return {"error": f"инструмент {name!r} упал: {exc}"}
    return {"error": f"неизвестный инструмент {name!r}, доступны: {', '.join(_TOOL_NAMES)}"}


def _complete(llm: LLMProvider, messages: list[dict], schema: dict, max_tokens: int) -> str:
    """Вызов модели с поднятым потолком бюджета; провайдер без `budget_cap`
    (тестовые заглушки) зовётся по-старому."""
    try:
        return llm.complete(messages, schema=schema, max_tokens=max_tokens, budget_cap=WRITER_BUDGET_CAP)
    except TypeError:
        return llm.complete(messages, schema=schema, max_tokens=max_tokens)


def _why(exc: BaseException) -> str:
    """Короткая причина отказа для отчёта, с вырезанными секретами:
    причина уходит в находки, отчёт и веб-интерфейс."""
    text = f"{type(exc).__name__}: {str(exc)[:200]}"
    for name in ("YANDEX_API_KEY", "YANDEX_FOLDER_ID"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "***")
    return text


def _parse_slide(data: dict, contract: SlideContract) -> SlideSpec:
    """Ответ модели как слайд: вид и раскладка из контракта, лишние поля
    выбора раскладки отброшены."""
    data = {k: v for k, v in data.items() if k != "layout_id"}
    data["kind"] = contract.kind
    slide = slide_spec_from_dict(data, contract.slide_id)
    return replace(slide, pattern_id=contract.pattern_id)


def _write_with_agent_loop(
    prompt_body: str, payload: dict, contract: SlideContract, llm: LLMProvider, profile, source_text: str,
    *, max_steps: int,
) -> tuple[SlideSpec | None, str | None]:
    """Агентный цикл одного слайда: на каждом шаге, кроме последнего,
    модель вправе ответить вызовом инструментов; любой другой объект
    трактуется как финальный слайд. Возвращает `(слайд, причина_отказа)`."""
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
                 "result": _run_tool_call(call, profile, contract, source_text)}
                for call in tool_calls
            ]
            conversation.append({"role": "assistant", "content": raw})
            conversation.append({"role": "user", "content": json.dumps({"tool_results": results}, ensure_ascii=False)})
            continue
        try:
            return _parse_slide(data, contract), None
        except Exception as exc:
            return None, f"шаг {step}/{max_steps}: ответ не лёг в схему слайда — {_why(exc)}"
    return None, f"бюджет шагов исчерпан ({max_steps}), финального слайда модель так и не прислала"


def _ask_repair(
    prompt_body: str, payload: dict, contract: SlideContract, llm: LLMProvider, previous: dict | None,
    problems: list[str],
) -> tuple[SlideSpec | None, str | None]:
    """Один ремонтный вызов: модель видит свой ответ и нарушения, списком,
    и переписывает слайд под контракт. Инструменты недоступны."""
    request = dict(payload)
    request["contract_problems"] = problems
    if previous is not None:
        request["previous_answer"] = previous
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]
    try:
        raw = _complete(llm, messages, _SLIDE_SCHEMA, WRITER_MAX_TOKENS)
    except Exception as exc:
        return None, f"модель не ответила — {_why(exc)}"
    try:
        return _parse_slide(json.loads(raw), contract), None
    except Exception as exc:
        return None, f"ответ не лёг в схему слайда — {_why(exc)}"


def _answer_view(slide: SlideSpec) -> dict:
    return {k: v for k, v in slide_spec_to_dict(slide).items() if k not in ("index", "findings", "pattern_id", "kind")}


def _fallback_slide(contract: SlideContract, *, reason: str | None = None) -> SlideSpec:
    """Детерминированный запасной слайд под контракт: пункты плана
    (`needs`) ложатся в главное место раскладки той формой, какой оно
    ждёт. Заведомо проходит `slide_spec_problems`: цифра в заголовке
    получает честную строку источника, а не выдуманную ссылку."""
    headline = contract.intent.strip() or "Слайд требует содержания"
    finding = "Слайд собран запасным вариантом — модель недоступна или не вернула валидный ответ."
    if reason:
        finding += f" Причина: {reason}."
    needs = [n for n in contract.evidence if n.strip()]
    if needs:
        finding += f" Нужны данные: {', '.join(needs)}."
    source_note = "Источник не подтверждён — текст запасного варианта, требует проверки перед показом." \
        if _DIGIT_RE.search(headline) else None
    main = next((s for s in contract.slots if s.required), None)
    blocks: list = []
    # На обложке и разделителе слайд с одним заголовком норма вёрстки.
    # На содержательном слайде пункты плана честнее пустоты.
    if needs and main is not None and contract.outline_kind not in ("title", "closing"):
        if main.block == "cards" and len(needs) >= 2:
            blocks = [CardBlock(items=[Card(body=n) for n in needs[:main.count]])]
        elif main.block == "text":
            blocks = [TextBlock(text="; ".join(needs))]
        else:
            blocks = [BulletBlock(items=list(needs))]
    return SlideSpec(
        index=contract.slide_id, kind=contract.kind, headline=headline, blocks=blocks,
        source_note=source_note, findings=[finding], pattern_id=contract.pattern_id,
        speaker_notes="Слайд собран запасным вариантом без модели: пункты взяты из плана, проверьте данные перед показом.",
    )


def _divider_slide(contract: SlideContract) -> SlideSpec:
    return SlideSpec(
        index=contract.slide_id, kind=contract.kind, headline=contract.divider_label or "",
        pattern_id=contract.pattern_id,
    )


def _with_photo(slide: SlideSpec, contract: SlideContract) -> SlideSpec:
    """Фото контент-пакета, назначенное слайду до планирования: раскладка
    уже выбрана с местом под картинку, модель фото не выбирает."""
    if not contract.photo:
        return slide
    if slide.visual is not None and slide.visual.kind in ("table", "chart"):
        return slide
    caption = (slide.visual.caption if slide.visual is not None else None) or contract.photo_caption
    return replace(slide, visual=Visual(kind="photo", caption=caption, photo_name=contract.photo))


def _write_one_slide(
    contract: SlideContract, profile, prompt_body: str, source_text: str, total: int, llm: LLMProvider | None,
    *, agent_max_steps: int = AGENT_MAX_STEPS_DEFAULT, style: str | None = None, log: list[dict] | None = None,
) -> SlideSpec:
    """Пишет один слайд. Ничего не трогает снаружи, кроме `log` (append из
    нескольких потоков под GIL безопасен), поэтому зовётся из пула."""
    if contract.is_divider:
        return _divider_slide(contract)
    payload = {
        "contract": contract.to_prompt(),
        "sources": source_text,
        "position": {"index": contract.slide_id, "total": total},
    }
    if style:
        payload["style"] = style

    slide: SlideSpec | None = None
    reason: str | None = None
    entry = {"index": contract.slide_id, "repair": None}
    if llm is None:
        reason = "модель не подключена (нет ключа) — текст слайдов не писался вовсе"
    else:
        slide, reason = _write_with_agent_loop(
            prompt_body, payload, contract, llm, profile, source_text, max_steps=agent_max_steps,
        )
        if slide is not None:
            schema_problems = slide_spec_problems(slide)
            problems = schema_problems + contract_problems(slide, contract)
            if problems:
                repaired, repair_reason = _ask_repair(
                    prompt_body, payload, contract, llm, _answer_view(slide), problems,
                )
                repaired_problems = None
                if repaired is not None and not slide_spec_problems(repaired):
                    repaired_problems = contract_problems(repaired, contract)
                accepted = repaired_problems is not None and (
                    schema_problems or len(repaired_problems) < len(problems)
                )
                entry["repair"] = "принят" if accepted else "отброшен"
                if accepted:
                    slide = repaired
                elif schema_problems:
                    slide = None
                    reason = (
                        f"ответ не прошёл проверку ({'; '.join(schema_problems)}), "
                        f"попытка исправить: {repair_reason or 'не помогла'}"
                    )
    if slide is None:
        slide = _fallback_slide(contract, reason=reason)
    else:
        left = contract_problems(slide, contract)
        entry["compliant"] = not left
        if left:
            slide.findings.append(
                f"Слайд {contract.slide_id}: текст не уложился в контракт раскладки "
                f"{contract.pattern_id}: {'; '.join(left[:3])}."
            )
    if contract.gap_note:
        slide.findings.append(contract.gap_note)
    slide = _with_photo(slide, contract)
    ok, places = contract_fill(slide, contract)
    entry.update(ok=ok, places=places)
    if log is not None:
        log.append(entry)
    return slide


def write_slides(
    outline: Outline, contracts: list[SlideContract], sources: list[SourceDoc], profile, llm: LLMProvider | None,
    *, max_workers: int = DEFAULT_WRITER_MAX_WORKERS, agent_max_steps: int = AGENT_MAX_STEPS_DEFAULT,
    style=None,
) -> DeckSpec:
    """Текст всех слайдов колоды под их контракты, параллельно.

    Порядок слайдов в колоде не зависит от того, кто ответил первым:
    результат собирается по номеру. Отказ одного слайда не роняет колоду.
    После письма: тонкий повтор факта выбрасывается, похожие заголовки
    помечаются, содержание вырождается под шаблон (`plan.normalize`: одна
    карточка в абзац, два пункта с числами в показатели); слайду, чья
    форма от этого поменялась, раскладка подбирается заново тем же
    расчётом стоимости, что у планировщика (`pattern.repick_pattern`).

    В `DeckSpec.meta`: сколько мест контракта заполнено в его пределах
    (`contract_places_ok`/`contract_places`), сколько было ремонтов."""
    _meta, prompt_body = _load_agent_prompt(AGENT_PATH_WRITER)
    source_text = "\n\n".join(f"### {s.name}\n{s.text}" for s in sources)
    total = len(contracts)
    style_value = getattr(style, "value", style)

    slides: list[SlideSpec | None] = [None] * total
    log: list[dict] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(
                _write_one_slide, contract, profile, prompt_body, source_text, total, llm,
                agent_max_steps=agent_max_steps, style=style_value, log=log,
            ): i
            for i, contract in enumerate(contracts)
        }
        for future in as_completed(futures):
            slides[futures[future]] = future.result()

    deck = DeckSpec(title=outline.title, language=outline.language, slides=list(slides))  # type: ignore[arg-type]
    deck.meta["contract_places"] = str(sum(e.get("places", 0) for e in log))
    deck.meta["contract_places_ok"] = str(sum(e.get("ok", 0) for e in log))
    repairs = [e for e in log if e.get("repair")]
    deck.meta["contract_repairs"] = str(len(repairs))
    deck.meta["contract_repairs_accepted"] = str(sum(1 for e in repairs if e["repair"] == "принят"))
    deck.meta["write_seconds"] = f"{time.monotonic() - started:.1f}"
    compliant = frozenset(e["index"] for e in log if e.get("compliant"))
    deck = normalize_deck(deck, profile, keep=compliant)
    if style_value and profile is not None:
        deck = _repick_changed(deck, profile, style_value)
    return replace(deck, slides=_dedupe_content(deck.slides, contracts))


def _dedupe_content(slides: list[SlideSpec], contracts: list[SlideContract]) -> list[SlideSpec]:
    """Повторы фактов ищутся только среди содержательных слайдов:
    разделители airy («Дальше — цифры», «Дальше — план») делят слово
    «Дальше» и без этого выбрасывались бы как тонкие повторы друг друга."""
    dividers = {id(slides[i]) for i, c in enumerate(contracts) if c.is_divider and i < len(slides)}
    kept = _drop_thin_duplicates(slides, frozenset(i for i, s in enumerate(slides) if id(s) in dividers))
    # `_drop_thin_duplicates` перенумеровывает выжившие копиями; разделитель
    # узнаётся по тому же заголовку без содержания.
    labels = {s.headline for s in slides if id(s) in dividers}
    skip = frozenset(i for i, s in enumerate(kept) if s.headline in labels and not s.blocks)
    _flag_repeated_headlines(kept, skip)
    return kept


def _repick_changed(deck: DeckSpec, profile, style: str) -> DeckSpec:
    """Слайды, у которых нормализация сбросила раскладку, получают новую:
    соседи уже назначены и остаются как есть."""
    ids = [s.pattern_id for s in deck.slides]
    if all(ids):
        return deck
    slides = list(deck.slides)
    for i, slide in enumerate(slides):
        if slide.pattern_id:
            continue
        pid = repick_pattern(slide, i, ids, profile, style)
        if pid is None:
            continue
        kind = next(p.kind for p in profile.patterns if p.pattern_id == pid)
        slides[i] = replace(slide, pattern_id=pid, kind=kind)
        ids[i] = pid
    return replace(deck, slides=slides)


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


def _drop_thin_duplicates(slides: list[SlideSpec], skip: frozenset[int] = frozenset()) -> list[SlideSpec]:
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
            if i in skip or j in skip:
                continue
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
    # Выброшенный слайд забирает с собой разделитель airy перед ним: иначе
    # два разделителя встали бы подряд.
    dropped |= {k - 1 for k in dropped if k - 1 in skip}
    kept = [s for k, s in enumerate(slides) if k not in dropped]
    return [replace(s, index=k) for k, s in enumerate(kept)]


def _flag_repeated_headlines(slides: list[SlideSpec], skip: frozenset[int] = frozenset()) -> None:
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
            if i in skip or j in skip:
                continue
            a, b = word_sets[i], word_sets[j]
            word_overlap = len(a & b) / min(len(a), len(b)) if a and b else 0.0
            shared_numbers = number_sets[i] & number_sets[j]
            if word_overlap >= _HEADLINE_OVERLAP_THRESHOLD or shared_numbers:
                slides[i].findings.append(
                    f"Слайд {slides[i].index}: заголовок похож на слайд {slides[j].index} "
                    f"({slides[j].headline!r}) — возможно, один и тот же факт другими словами."
                )

