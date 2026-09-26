"""Одиннадцать недетерминированных проверок содержания слайда по картинке
(Task 12, Приложение 1 ТЗ, раздел «Валидация контента»).

ТЗ дословно: «Недетерминированная проверка касается смысла, поэтому
выполняется моделью и на повторных запусках может дать разный ответ» —
`run_visual` намеренно НЕ обещает то же, что `audit.deterministic.
run_deterministic` (один и тот же результат при любом числе повторов). Это
не баг этого модуля, а прямое следствие того, ЧТО он проверяет.

## Девять вопросов на слайд, два — на колоду целиком

Промпт `agents/content-auditor/AGENT.md` перечисляет все одиннадцать
вопросов дословно из ТЗ (версия несётся фронтматтером файла, тот же приём,
что и `palette-namer`/`outline-writer`/`slide-writer`/`pattern-picker`) —
но не каждый вопрос задаётся на каждом слайде. C09 («вся колода на одном
языке») и C11 («соседние слайды связаны между собой по логике») спрашивают
про колоду ЦЕЛИКОМ, и задавать их отдельно на каждом слайде — тот же смысл
одиннадцать раз, не разный: 12 слайдов означали бы 12 независимых (и,
поскольку модель недетерминирована, возможно противоречащих друг другу)
ответов на один и тот же факт. Эта функция спрашивает их РОВНО один раз —
одним отдельным вызовом модели поверх склеенной "простыни" уменьшенных
превью всех слайдов подряд (`_build_collage`), а не текстом (ТЗ:
«на вход — картинка слайда», и это правило нарушается меньше всего именно
так — контроль связности и языка тоже смотрит на картинки, просто на все
разом, а не на одну).

Остальные девять (C01–C08, C10) спрашиваются на КАЖДОМ слайде, одним
вызовом модели на слайд (а не девятью — `VisionProvider.ask_image` и так
не бесплатен по времени, см. `provider/yandex.py`), пачками по
`max_workers` слайдов параллельно.

Итог по числу вызовов модели на колоду из N слайдов: N (по одному на
слайд) + 1 (коллаж на всю колоду) = N+1, а не 11×N.

## Честная деградация без модели

ТЗ требует (бриф задачи, «Что важно»): «Без модели аудит обязан честно
деградировать. Не возвращать пустой список, делая вид, что всё хорошо, а
говорить, что проверка не выполнялась и почему.» `VisualAuditResult.
skipped_reason` — то самое "почему": непустая строка, если проверка не
запускалась вовсе (нет провайдера или выбранная модель не мультимодальна),
`None` — если аудит реально прошёл (даже если не нашёл ни одной находки:
пустой `findings` при `skipped_reason is None` — законный результат
"проверка выполнялась и ничего не нашла", а не молчание).

## Невалидный ответ модели — находка, не исключение

ТЗ (бриф, «Что важно»): «Невалидный ответ модели не должен ронять
пайплайн. Ответ валидируется схемой; не разобрался — это отдельная находка
с текстом ответа, а не исключение.» `_parse_answer` — единственное место,
где формат ответа проверяется; любая её ошибка (JSON не разобрался, нет
ожидаемого ключа, `ok` не булево) ловится вызывающим кодом (`_run_one_
slide`/`_run_deck_level`) и превращается в один finding `check_id="C00"` с
текстом сырого ответа модели — как и сетевая/любая другая ошибка вызова
`ask_image` самого по себе (недоступность модели посреди аудита колоды не
должна обрывать проверку оставшихся слайдов)."""
from __future__ import annotations
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from PIL import Image, ImageDraw, ImageFont

from deckforge.audit.findings import Finding, Severity
from deckforge.plan.outline import SourceDoc
from deckforge.plan.spec import (
    BulletBlock, CardBlock, DeckSpec, KpiBlock, QuoteBlock, SlideSpec, TextBlock,
)
from deckforge.provider.base import VisionProvider
from deckforge.template.profile import TemplateProfile

AGENT_PATH = Path(__file__).resolve().parents[3] / "agents" / "content-auditor" / "AGENT.md"

PER_SLIDE_CHECK_IDS: tuple[str, ...] = ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C10")
DECK_LEVEL_CHECK_IDS: tuple[str, ...] = ("C09", "C11")
CHECK_IDS: tuple[str, ...] = tuple(sorted(PER_SLIDE_CHECK_IDS + DECK_LEVEL_CHECK_IDS))

# Дефолт интерфейса брифа (`ask_image(..., max_tokens=1024)`) рассчитан на
# короткий текстовый ответ, не на JSON-объект из девяти ключей с
# развёрнутым "where" у каждого "нет" — на qwen3.6 (рассуждающая модель,
# см. провенанс чисел в `provider/yandex.py`/`plan/writer.py`) это почти
# гарантированно уходит в finish_reason=length. Хуже того: `ask_image`
# (в отличие от `complete(..., schema=...)`) не сообщает провайдеру, что
# ждёт JSON (`expects_json` в `_post_with_budget_escalation` остаётся
# `False`), поэтому автоматическая эскалация бюджета там не срабатывает на
# ПУСТОМ content, если он УЖЕ на потолке `MAX_TOKENS_BUDGET_CAP` (6144,
# `provider/yandex.py`): эскалация умножает текущий бюджет на два и берёт
# `min(..., MAX_TOKENS_BUDGET_CAP)` — если начать РОВНО с потолка,
# `next_tokens <= body["max_tokens"]` истинно с первого шага, и эскалация
# не делает ни одной попытки.
#
# Живой замер (обязательная проверка задачи, `task-12-report.md`):
# `max_tokens=6144` на слайде из одного заголовка (простейший случай) ушёл
# ЦЕЛИКОМ в reasoning_content, content пуст, `RuntimeError` без единого
# символа ответа — то есть ПОТОЛКА РОЛЕЙ КОНВЕЙЕРА (palette_namer/
# slide-writer/pattern_picker, все делят один и тот же `MAX_TOKENS_BUDGET_
# CAP`) НЕ ХВАТАЕТ даже на самый простой слайд этой роли. На `max_tokens=
# 16000` тот же запрос отдал полный валидный ответ за 46.5с. На `max_tokens=
# 20000` слайд с двумя буллетами и сверкой цифр с исходными материалами
# (C04) отдал ответ за 17.4с. Значения ниже НЕ экономят время ответа модели
# (та отвечает, как только рассуждение закончилось, а не ждёт потолка) —
# они только повышают риск получить пустой/обрезанный content и находку
# `C00` вместо реального вердикта. Раз `ask_image` принимает `max_tokens`
# от вызывающего кода НАПРЯМУЮ (не через `complete()`), а `MAX_TOKENS_
# BUDGET_CAP` ограничивает только шаг ЭСКАЛАЦИИ (не стартовое значение —
# см. `_post_with_budget_escalation` в `provider/yandex.py`), эта роль
# сознательно стартует ВЫШЕ общего потолка остальных ролей конвейера — со
# своим запасом, не заимствуя чужой.
_PER_SLIDE_MAX_TOKENS = 20480

# Коллаж C09/C11 (`_build_collage`) оказался ТЯЖЕЛЕЕ по бюджету, чем один
# слайд — та же "простыня" из двух миниатюр на `max_tokens=20000` ушла
# целиком в reasoning_content без единого символа ответа (тот же класс
# отказа, что и выше), а на `max_tokens=32000` отдала валидный ответ за
# 24.4с. Разумное объяснение: модели труднее разобрать композитную картинку
# с несколькими врезанными подписями номеров слайдов, чем один цельный
# рендер — не то же самое изображение, что видела на предыдущем вопросе, и
# reasoning тратится заново на его разбор. Запас больше, чем у
# `_PER_SLIDE_MAX_TOKENS`, той же логикой (замер, не гадание).
_DECK_LEVEL_MAX_TOKENS = 40960

_CHECK_LABEL: dict[str, str] = {
    "C01": "Заголовок не содержит вывод, а просто называет тему",
    "C02": "Содержимое слайда не соответствует заголовку",
    "C03": "Слайд не пересказывается одним предложением",
    "C04": "Не все цифры и факты со слайда есть в исходных материалах",
    "C05": "На слайде нет содержания, только заголовок",
    "C06": "Картинки/иконки не относятся к теме слайда",
    "C07": "На слайде служебный мусор (реплики спикера, куски промпта)",
    "C08": "В тексте есть опечатки",
    "C09": "Колода не на одном языке",
    "C10": "Не все строки таблицы/элементы легенды работают на мысль слайда",
    "C11": "Соседние слайды не связаны между собой по логике",
}

# Тот же трёхуровневый принцип, что и `audit/findings.py::Severity`
# (докстрока модуля — не повторяем её здесь дословно): "critical" — виден
# без анализа и ломает профессиональный вид сам по себе; "major" — явное
# расхождение с содержанием/дизайном, замечаемое при чтении; "minor" —
# требует контекста, чтобы решить, брак это или осознанное решение автора.
#
# C07 (реплики спикера/куски промпта на слайде) — единственный critical:
# это не требует анализа контекста ВООБЩЕ, видно с первого взгляда и рушит
# профессиональный вид ровно как "слайд подменён скриншотом" из докстроки
# `findings.py`. C01/C02/C04/C05/C08/C09 — major: расхождение с
# содержанием/языком, которое читатель заметит, но которое не делает файл
# нечитаемым. C03/C06/C10/C11 — minor: субъективнее (одно ли предложение
# уложило бы смысл, к месту ли иконка, работает ли строка таблицы на мысль,
# связаны ли слайды) — тот класс, где нужен контекст, чтобы решить, брак
# это или осознанный выбор автора.
_CHECK_SEVERITY: dict[str, Severity] = {
    "C01": "major", "C02": "major", "C03": "minor", "C04": "major", "C05": "major",
    "C06": "minor", "C07": "critical", "C08": "major", "C09": "major",
    "C10": "minor", "C11": "minor",
}

_CHECK_FIX_HINT: dict[str, str] = {
    "C01": "Переписать заголовок так, чтобы он называл вывод, а не тему.",
    "C02": "Привести содержимое слайда в соответствие с заголовком или переписать заголовок.",
    "C03": "Сократить слайд до одной ясной мысли.",
    "C04": "Проверить цифру по исходным материалам; убрать или подтвердить источником.",
    "C05": "Добавить содержание — заголовка недостаточно для этого типа слайда.",
    "C06": "Заменить картинку/иконку на относящуюся к теме слайда.",
    "C07": "Убрать служебный текст (реплики спикера, куски промпта) со слайда.",
    "C08": "Исправить опечатку.",
    "C09": "Перевести слайд(ы) на единый язык колоды.",
    "C10": "Убрать или переработать строку/элемент легенды, не работающие на мысль слайда.",
    "C11": "Проверить переход между слайдами — добавить связку по смыслу.",
}

_MALFORMED_FIX_HINT = "Повторить вызов модели или проверить слайд вручную — автоматический вердикт недоступен."

# Ширина одной миниатюры в "простыне" C09/C11 (`_build_collage`) — округлое
# число, при котором на типичной колоде 10-15 слайдов (16:9) итоговый PNG
# остаётся в единицах мегабайт после кодирования base64 (см. `ask_image`,
# `provider/yandex.py`), не десятках: и модель, и HTTP-клиент обязаны
# уложиться в разумное время одного запроса.
_COLLAGE_WIDTH_PX = 480
_COLLAGE_GAP_PX = 6
_COLLAGE_LABEL_HEIGHT_PX = 18


SlideScore = dict[str, "int | str"]
"""Оценки одного слайда по PPTEval: подмножество ключей `content`/`design`
(int 1-5) и `why` (str, одна фраза) — какие из них реально пришли валидными
в ответе модели, те и есть; `_parse_scores` тихо отбрасывает остальное
(докстрока `_parse_scores`), поэтому ловить отсутствие ключа нужно через
`.get`, не через прямой индекс."""

DeckScore = dict[str, "int | str"]
"""То же самое, но для колоды целиком: ключи `coherence` (int 1-5) и `why`."""

_SLIDE_SCORE_NUM_KEYS: tuple[str, ...] = ("content", "design")
_DECK_SCORE_NUM_KEYS: tuple[str, ...] = ("coherence",)


def _parse_scores(raw_scores: object, num_keys: tuple[str, ...]) -> dict[str, int | str] | None:
    """Оценки PPTEval — необязательная надстройка над да/нет-вопросами
    (задача G, бриф: «оценки необязательные, вне диапазона отбрасываются,
    ошибки не роняют аудит»). В отличие от `_parse_answer` эта функция
    НИКОГДА не бросает исключение — она часть того же ответа модели, что и
    C01-C11, и не должна превращать валидный (для да/нет-вопросов) ответ в
    находку `C00` только из-за того, что модель поставила оценку вне
    диапазона 1-5 или написала `why` не строкой. Возвращает `None`, если ни
    одного валидного поля не нашлось — вызывающий код тогда просто не
    записывает оценку за эту единицу (слайд/колоду), как и было бы, если
    бы модель вовсе не прислала `scores`."""
    if not isinstance(raw_scores, dict):
        return None
    out: dict[str, int | str] = {}
    for key in num_keys:
        value = raw_scores.get(key)
        if isinstance(value, bool):
            continue  # bool — подкласс int в Python, но это не оценка 1-5
        if isinstance(value, int) and 1 <= value <= 5:
            out[key] = value
    why = raw_scores.get("why")
    if isinstance(why, str) and why.strip():
        out["why"] = why.strip()
    return out or None


def _extract_scores(raw: str | None, num_keys: tuple[str, ...]) -> dict[str, int | str] | None:
    """Достаёт `data["scores"]` из сырого ответа модели независимо от
    `_parse_answer` (см. её докстроку про C01-C11) — тот же сырой текст
    несёт оба: обязательные да/нет-ключи и необязательный `scores`. Любая
    ошибка разбора здесь (не JSON, не объект) молча даёт `None` — оценки не
    обязаны быть в ответе, а C00 за них уже не начисляется (это сделала бы
    `_parse_answer`, если бы упала на обязательных ключах)."""
    if raw is None:
        return None
    try:
        data = json.loads(_strip_markdown_fence(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _parse_scores(data.get("scores"), num_keys)


@dataclass
class VisualAuditResult:
    """Результат визуального аудита колоды — обёртка над `list[Finding]`,
    а не голый список: интерфейс брифа (`run_visual(...) -> list[Finding]`)
    упрощён до сути, реальный вызывающий код нуждается ЕЩЁ и в
    `skipped_reason` (честная деградация без модели, докстрока модуля) —
    голый список не может нести это поле, не притворяясь при этом
    "аудит прошёл и находок нет". Итерируется и меряется `len()` как
    список находок — большинство вызывающего кода (`AuditReport.merge`,
    тесты брифа вида `{f.check_id for f in findings}`) не обязано знать про
    эту обёртку."""

    findings: list[Finding] = field(default_factory=list)
    skipped_reason: str | None = None
    slides_checked: int = 0
    model_calls: int = 0
    elapsed_seconds: float = 0.0
    # Задача G (PPTEval: Content/Design/Coherence 1-5 с обоснованием) —
    # необязательная надстройка поверх C01-C11: слайд, за который модель не
    # прислала валидную оценку (докстрока `_parse_scores`), просто не несёт
    # записи в `slide_scores`, `deck_score` остаётся `None`, а средние —
    # `None`, если считать не по чему. Пустая надстройка не портит основной
    # результат (`findings`/`skipped_reason`) — те же гарантии, что и у
    # самого визуального аудита при недоступности модели.
    slide_scores: dict[int, "SlideScore"] = field(default_factory=dict)
    deck_score: "DeckScore | None" = None
    content_avg: float | None = None
    design_avg: float | None = None

    def __iter__(self):
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)


def _supports_vision(vlm) -> bool:
    if vlm is None:
        return False
    card = getattr(vlm, "card", None)
    if card is not None and hasattr(card, "vision"):
        return bool(card.vision)
    return isinstance(vlm, VisionProvider)


def _load_agent_prompt(path: Path = AGENT_PATH) -> tuple[dict, str]:
    """Тот же приём, что `plan/writer.py::_load_agent_prompt`/`template/
    naming.py::_load_agent_prompt` — каждый потребитель читает СВОЙ
    AGENT.md сам, независимо (см. докстроку `deterministic.py` про
    намеренное неразделение маленьких формул между независимыми
    модулями — тот же принцип, применённый к пяти строкам парсинга
    фронтматтера)."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path}: ожидался YAML-фронтматтер, ограниченный `---`")
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


def _join_sources(sources: list[SourceDoc] | None) -> str:
    if not sources:
        return "(исходные материалы не переданы — проверку C04 по ним провести нечем)"
    return "\n\n".join(f"### {s.name}\n{s.text}" for s in sources)


def _block_text(block) -> str:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, BulletBlock):
        return "\n".join(f"- {item}" for item in block.items)
    if isinstance(block, CardBlock):
        return "\n".join(f"- {c.title}: {c.body}" if c.title else f"- {c.body}" for c in block.items)
    if isinstance(block, KpiBlock):
        return "; ".join(f"{k.value} — {k.label}" for k in block.items)
    if isinstance(block, QuoteBlock):
        return f"«{block.text}»" + (f" — {block.author}" if block.author else "")
    return ""


def _slide_digest(slide: SlideSpec) -> str:
    """Компактный текстовый пересказ содержания слайда — только для
    служебного контекста ("текст соседних слайдов" промпта AGENT.md), не
    замена картинке: сами девять/два вопроса модель отвечает по
    изображению, докстрока модуля."""
    parts = [f"Заголовок: {slide.headline}"]
    if slide.subhead:
        parts.append(f"Подзаголовок: {slide.subhead}")
    for block in slide.blocks:
        text = _block_text(block)
        if text:
            parts.append(text)
    if slide.visual is not None and slide.visual.caption:
        parts.append(f"Подпись визуала: {slide.visual.caption}")
    if slide.source_note:
        parts.append(f"Источник: {slide.source_note}")
    return "\n".join(parts)


def _neighbor_context(pairs: list[tuple], index: int) -> dict:
    ctx = {}
    if index > 0:
        ctx["previous_slide"] = _slide_digest(pairs[index - 1][1])
    if index + 1 < len(pairs):
        ctx["next_slide"] = _slide_digest(pairs[index + 1][1])
    return ctx


def _build_slide_prompt(
    agent_body: str, index: int, total: int, spec: DeckSpec, slide: SlideSpec,
    pairs: list[tuple], source_text: str,
) -> str:
    payload = {
        "answer_only_keys": list(PER_SLIDE_CHECK_IDS),
        "note": (
            "Это слайд из колоды — отвечай ТОЛЬКО на перечисленные в answer_only_keys "
            "ключи (вопросы 1-8 и 10). Вопросы про колоду целиком (9 и 11) задаются "
            "отдельным запросом на всю колоду и здесь не нужны."
        ),
        "slide_index_1based": index + 1,
        "total_slides": total,
        "deck_title": spec.title,
        "deck_language": spec.language,
        "source_materials": source_text,
        **_neighbor_context(pairs, index),
    }
    return f"{agent_body}\n\nСлужебные данные:\n{json.dumps(payload, ensure_ascii=False)}"


def _build_deck_prompt(agent_body: str, spec: DeckSpec, pairs: list[tuple]) -> str:
    payload = {
        "answer_only_keys": list(DECK_LEVEL_CHECK_IDS),
        "note": (
            "На картинке — уменьшенные превью ВСЕХ слайдов колоды подряд сверху вниз, "
            "каждое подписано номером слайда. Отвечай ТОЛЬКО на ключи из answer_only_keys "
            "(вопросы 9 и 11) — про колоду целиком, не про один слайд."
        ),
        "deck_title": spec.title,
        "deck_language": spec.language,
        "slides": [
            {"index_1based": i + 1, "headline": s.headline} for i, (_p, s) in enumerate(pairs)
        ],
    }
    return f"{agent_body}\n\nСлужебные данные:\n{json.dumps(payload, ensure_ascii=False)}"


def _build_collage(pngs: list[Path]) -> bytes:
    """Одна картинка — вертикальная "простыня" уменьшенных превью всех
    слайдов по порядку, каждое подписано номером. Нужна ТОЛЬКО C09/C11
    (докстрока модуля — они спрашиваются один раз на колоду, а
    `VisionProvider.ask_image` принимает ровно одну картинку за вызов, не
    список)."""
    import io

    thumbs: list[Image.Image] = []
    for path in pngs:
        with Image.open(path) as im:
            im = im.convert("RGB")
            ratio = _COLLAGE_WIDTH_PX / im.width
            thumbs.append(im.resize((_COLLAGE_WIDTH_PX, max(1, round(im.height * ratio)))))

    total_height = sum(t.height for t in thumbs) + _COLLAGE_GAP_PX * len(thumbs) + _COLLAGE_LABEL_HEIGHT_PX * len(thumbs)
    collage = Image.new("RGB", (_COLLAGE_WIDTH_PX, max(1, total_height)), color=(255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = ImageFont.load_default()

    y = 0
    for i, thumb in enumerate(thumbs, start=1):
        draw.text((2, y), f"Слайд {i}", fill=(0, 0, 0), font=font)
        y += _COLLAGE_LABEL_HEIGHT_PX
        collage.paste(thumb, (0, y))
        y += thumb.height + _COLLAGE_GAP_PX

    buf = io.BytesIO()
    collage.save(buf, format="PNG")
    return buf.getvalue()


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_markdown_fence(raw: str) -> str:
    """AGENT.md просит «никакого текста вне JSON» — qwen3.6 это правило
    не всегда соблюдает и заворачивает ответ в ```json ... ``` (живой
    прогон обязательной проверки задачи, `task-12-report.md`: ответ вида
    "```json\\n{...}\\n```" на самом простом слайде из всех). Без снятия
    ограды `json.loads` падает на первом символе ("```") — не невалидный
    формат данных ("ok" не булево и т.п.), а исключительно оформление,
    которое разумно снять, а не карать находкой `C00` наравне с реально
    сломанным ответом."""
    match = _FENCE_RE.match(raw.strip())
    return match.group(1).strip() if match else raw


def _parse_answer(raw: str, expected_keys: tuple[str, ...]) -> dict[str, tuple[bool, str | None]]:
    """Валидация ответа модели схемой (бриф, «Что важно»): любое
    отклонение от ожидаемой формы бросает `ValueError` с понятным для
    человека текстом — вызывающий код превращает его в находку `C00`, не
    даёт упасть пайплайну."""
    try:
        data = json.loads(_strip_markdown_fence(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"ответ не разобрался как JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"ожидался объект JSON, получено {type(data).__name__}")

    result: dict[str, tuple[bool, str | None]] = {}
    for key in expected_keys:
        entry = data.get(key)
        if not isinstance(entry, dict) or "ok" not in entry or not isinstance(entry["ok"], bool):
            raise ValueError(f"ключ {key!r} отсутствует в ответе или имеет неверный формат")
        where = entry.get("where")
        if where is not None and not isinstance(where, str):
            raise ValueError(f"ключ {key!r}: поле where должно быть строкой")
        result[key] = (entry["ok"], where)
    return result


def _findings_from_answers(slide_index: int | None, answers: dict[str, tuple[bool, str | None]]) -> list[Finding]:
    out = []
    for check_id, (ok, where) in answers.items():
        if ok:
            continue
        label = _CHECK_LABEL[check_id]
        message = f"{label}: {where}" if where else label
        out.append(Finding(
            check_id=check_id, severity=_CHECK_SEVERITY[check_id], slide_index=slide_index,
            shape_ref=None, message=message, box=None, fixable=False,
            fix_hint=_CHECK_FIX_HINT[check_id],
        ))
    return out


def _malformed_finding(slide_index: int | None, exc: Exception, raw_answer: str | None) -> Finding:
    raw_note = f" Сырой ответ модели: {raw_answer[:500]!r}" if raw_answer else ""
    return Finding(
        check_id="C00", severity="major", slide_index=slide_index, shape_ref=None,
        message=f"Ответ модели визуального аудита не прошёл валидацию схемой: {exc}.{raw_note}",
        box=None, fixable=False, fix_hint=_MALFORMED_FIX_HINT,
    )


# Замечено в живой проверке задачи (task-12-report.md, "Слайды 6 и 7 —
# самое важное наблюдение сессии"): именно на самых кривых слайдах колоды
# (где вердикт нужнее всего) модель чаще молчит вовсе — весь бюджет
# `max_tokens` уходит в `reasoning_content`. Ответ недетерминирован, и
# повторный запрос ТЕМ ЖЕ промптом часто отвечает содержательно там, где
# первый не ответил ничего (тот же живой прогон: три изолированных повтора
# после падения `test_missing_body_is_caught` сразу прошли успешно). Это
# НЕ замена наращиванию `max_tokens` (тот путь уже испробован — см.
# докстроки `_PER_SLIDE_MAX_TOKENS`/`_DECK_LEVEL_MAX_TOKENS` выше, — 20480/
# 40960 не гарантируют ответ ни на каком разумном потолке) — здесь второй
# ПОЛНЫЙ заход (свежий вызов `ask_image`, не переиспользование пустого
# ответа), а не третья, четвёртая... попытка: диминишинг ретёрн такой же,
# как у `MAX_BUDGET_ESCALATIONS` в `provider/yandex.py` (одна эскалация
# закрывает почти все случаи, вторая — редкий повторный перекос, дальше уже
# не "бюджета мало").
_MAX_MODEL_ATTEMPTS = 2


def _ask_and_parse_with_retry(
    ask: Callable[[], str], expected_keys: tuple[str, ...],
) -> tuple[dict[str, tuple[bool, str | None]] | None, Exception | None, str | None]:
    """До `_MAX_MODEL_ATTEMPTS` ПОЛНЫХ заходов (вызов модели + разбор
    ответа) по одному и тому же запросу — первый успешный разбор
    возвращается сразу, последняя ошибка/сырой ответ возвращаются, только
    если ВСЕ попытки не удались (вызывающий код превращает их в один
    `Finding(check_id="C00")`, не в один на попытку).

    Сырой текст ответа (`raw`) возвращается ВСЕГДА, когда модель хоть
    что-то ответила — не только при неудаче. Это нужно задаче G (оценки
    PPTEval, `_extract_scores`): `scores` лежит в том же JSON-объекте, что и
    C01-C11, и вызывающему коду нужен сырой текст успешного ответа тоже, а
    не только текст ответа, который не прошёл валидацию схемой."""
    last_exc: Exception | None = None
    last_raw: str | None = None
    for _attempt in range(_MAX_MODEL_ATTEMPTS):
        try:
            raw = ask()
        except Exception as exc:  # noqa: BLE001 — сеть/модель посреди аудита колоды не должна обрывать проверку остальных слайдов
            last_exc, last_raw = exc, None
            continue
        try:
            return _parse_answer(raw, expected_keys), None, raw
        except Exception as exc:  # noqa: BLE001 — см. докстроку модуля, "Невалидный ответ модели — находка, не исключение"
            last_exc, last_raw = exc, raw
    return None, last_exc, last_raw


def _run_one_slide(vlm, agent_body: str, index: int, total: int, spec: DeckSpec,
                    png_path: Path, slide: SlideSpec, pairs: list, source_text: str,
                    ) -> tuple[list[Finding], "SlideScore | None"]:
    prompt = _build_slide_prompt(agent_body, index, total, spec, slide, pairs, source_text)
    png_bytes = Path(png_path).read_bytes()
    answers, exc, raw = _ask_and_parse_with_retry(
        lambda: vlm.ask_image(png_bytes, prompt, max_tokens=_PER_SLIDE_MAX_TOKENS), PER_SLIDE_CHECK_IDS,
    )
    if answers is None:
        return [_malformed_finding(index, exc, raw)], None
    return _findings_from_answers(index, answers), _extract_scores(raw, _SLIDE_SCORE_NUM_KEYS)


def _run_deck_level(vlm, agent_body: str, spec: DeckSpec, pngs: list[Path], pairs: list,
                     ) -> tuple[list[Finding], "DeckScore | None"]:
    try:
        collage = _build_collage(pngs)
    except Exception as exc:  # noqa: BLE001 — сборка коллажа сама (не сеть) не должна обрывать остальной аудит
        return [_malformed_finding(None, exc, None)], None
    prompt = _build_deck_prompt(agent_body, spec, pairs)
    answers, exc, raw = _ask_and_parse_with_retry(
        lambda: vlm.ask_image(collage, prompt, max_tokens=_DECK_LEVEL_MAX_TOKENS), DECK_LEVEL_CHECK_IDS,
    )
    if answers is None:
        return [_malformed_finding(None, exc, raw)], None
    return _findings_from_answers(None, answers), _extract_scores(raw, _DECK_SCORE_NUM_KEYS)


def run_visual(
    pngs: list[Path],
    spec: DeckSpec,
    profile: TemplateProfile,
    vlm,
    *,
    sources: list[SourceDoc] | None = None,
    max_workers: int = 4,
) -> VisualAuditResult:
    """Одиннадцать недетерминированных проверок (`CHECK_IDS`) готовой
    колоды по картинке каждого слайда — интерфейс брифа дословно
    (`run_visual(pngs, spec, profile, vlm) -> list[Finding]`), расширенный
    `sources`/`max_workers` (докстрока модуля) и оборачивающий результат в
    `VisualAuditResult`, а не голый список (см. её докстроку).

    `profile` принят, но не используется: дизайн-система шаблона (цвета,
    сетка, шрифты) — область детерминированного аудита
    (`audit.deterministic`), визуальный аудит смотрит на СМЫСЛ картинки, не
    на её геометрию/палитру. Тот же принцип "параметр интерфейса шире, чем
    нужно одному конкретному шагу", что и `profile` в `plan.outline.
    build_outline` (см. её докстроку)."""
    del profile
    started = time.monotonic()

    if vlm is None:
        return VisualAuditResult(
            skipped_reason=(
                "модель для визуального аудита не задана (нет ключа/провайдера) — "
                "проверка C01-C11 не выполнялась"
            ),
        )
    if not _supports_vision(vlm):
        model_name = getattr(vlm, "model_uri", None) or getattr(vlm, "model", None) or type(vlm).__name__
        return VisualAuditResult(
            skipped_reason=(
                f"выбранная модель ({model_name}) не мультимодальна — визуальный аудит по "
                "картинке невозможен, проверка C01-C11 не выполнялась"
            ),
        )

    _meta, agent_body = _load_agent_prompt()
    source_text = _join_sources(sources)
    pairs = list(zip(pngs, spec.slides))
    total = len(pairs)

    findings: list[Finding] = []
    slide_scores: dict[int, SlideScore] = {}
    deck_score: DeckScore | None = None
    calls = 0

    if total:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = {
                pool.submit(
                    _run_one_slide, vlm, agent_body, i, total, spec, png_path, slide, pairs, source_text,
                ): i
                for i, (png_path, slide) in enumerate(pairs)
            }
            for future in as_completed(futures):
                calls += 1
                slide_index = futures[future]
                slide_findings, slide_score = future.result()
                findings.extend(slide_findings)
                if slide_score is not None:
                    slide_scores[slide_index] = slide_score

        calls += 1
        deck_findings, deck_score = _run_deck_level(vlm, agent_body, spec, pngs, pairs)
        findings.extend(deck_findings)

    findings.sort(key=lambda f: (f.slide_index if f.slide_index is not None else -1, f.check_id, f.message))

    return VisualAuditResult(
        findings=findings, skipped_reason=None, slides_checked=total,
        model_calls=calls, elapsed_seconds=time.monotonic() - started,
        slide_scores=slide_scores, deck_score=deck_score,
        content_avg=_axis_average(slide_scores, "content"),
        design_avg=_axis_average(slide_scores, "design"),
    )


def _axis_average(slide_scores: dict[int, "SlideScore"], axis: str) -> float | None:
    """Среднее по оси (`content`/`design`) среди слайдов, которые реально
    получили валидную оценку этой оси — не среди ВСЕХ слайдов колоды: часть
    слайдов может остаться без оценки (ответ модели не прислал `scores`,
    докстрока `_parse_scores`), и делить сумму на общее число слайдов
    занизило бы среднее теми, кто вообще не участвовал в оценке."""
    values = [s[axis] for s in slide_scores.values() if isinstance(s.get(axis), int)]
    if not values:
        return None
    return sum(values) / len(values)
