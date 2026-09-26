"""Структура колоды: сколько слайдов, о чём каждый, в каком порядке —
первый шаг планирования содержания (Task 13). Текст слайдов пишет
следующий шаг (`writer.write_slides`), конкретную раскладку — шаг за ним
(`writer.pick_patterns`/`variants.apply_variant`); этот модуль не знает ни
о том, ни о другом.

Промпт — файл `agents/outline-writer/AGENT.md`, не строка в коде (ТЗ требует
промпты файлами с версией во фронтматтере — тот же приём, что уже применён
`template/naming.py` для `agents/palette-namer/AGENT.md`, см. `_load_agent_
prompt` там). Код здесь читает файл, вызывает модель по схеме и, как и
`naming.py`, не доверяет ответу слепо: `kind` каждого предложенного слайда
обязан входить в закрытый список `OUTLINE_KINDS`, невалидный слайд
отбрасывается, а не портит остальную структуру; без модели (`llm=None`) или
при любом сбоя сети/парсинга — детерминированный запасной вариант, а не
падение (тот же принцип, что и у `name_palette_roles_report`)."""
from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from deckforge.provider.base import LLMProvider

logger = logging.getLogger(__name__)

AGENT_PATH = Path(__file__).resolve().parents[3] / "agents" / "outline-writer" / "AGENT.md"

# Закрытый список типов слайда структуры — AGENT.md, дословно.
OUTLINE_KINDS = (
    "title", "agenda", "context", "problem", "solution", "how_it_works",
    "data", "comparison", "case", "roadmap", "team", "risks", "ask", "closing",
)

# Объём колоды — ТЗ дословно ("10-15 слайдов или заданное пользователем").
MIN_SLIDES = 4
MAX_SLIDES = 15
_DEFAULT_TARGET_SLIDES = 6

# Бюджет токенов начального вызова. Изначально 4096 (тот же порядок, что
# `naming._PALETTE_NAMING_INITIAL_MAX_TOKENS`) — живой прогон обязательной
# проверки задачи (девять презентаций, см. отчёт) показал, что qwen3.6
# рассуждающая тратит на reasoning весь бюджет 4096 систематически, а не
# как редкое исключение: эскалация до `MAX_TOKENS_BUDGET_CAP`=6144
# (`provider/yandex.py`) срабатывала на КАЖДОМ вызове без исключения —
# значит для этой роли 4096 не "типичный случай с редким перекосом", а
# гарантированно заниженный старт, который только теряет время на лишний
# HTTP-круг. Начинать сразу с потолка эскалации не имеет смысла экономить —
# он всё равно будет достигнут.
OUTLINE_MAX_TOKENS = 6144


@dataclass(frozen=True)
class SourceDoc:
    """Один исходный документ (бриф, таблица цифр, riskи и т.п.) — план
    работает с текстом целиком, не разбирает его структуру сам (это делает
    модель, читая `sources` как есть)."""
    name: str
    text: str


@dataclass(frozen=True)
class OutlineSlide:
    kind: str
    intent: str
    needs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Outline:
    """Структура будущей колоды. `title`/`language` — не часть JSON-ответа
    модели (AGENT.md просит только `slides`), а метаданные колоды, которые
    несёт содержательный пакет (`brief.md`, фронтматтер) и которые нужны
    `writer.write_slides`, чтобы собрать `DeckSpec` целиком, не имея
    отдельного параметра под них в своей сигнатуре (брифом Task 13,
    "Interfaces" — `write_slides(outline, sources, profile, llm)`, без
    title/language отдельно) — поле сверх литерального перечня JSON-схемы
    брифа, но без него `DeckSpec.title`/`.language` физически неоткуда
    взять на выходе `write_slides`."""
    slides: list[OutlineSlide]
    title: str = ""
    language: str = "ru"
    generation_origin: str = "model"
    generation_error: str | None = None


class OutlineGenerationError(RuntimeError):
    """Модель не смогла подготовить структуру для обычного API-прогона."""


_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(OUTLINE_KINDS)},
                    "intent": {"type": "string"},
                    "needs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "intent"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["slides"],
    "additionalProperties": False,
}


def _load_agent_prompt(path: Path = AGENT_PATH) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path}: ожидался YAML-фронтматтер, ограниченный `---`")
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


def _clamp_target(target_slides: int | None, brief: str = "", sources: list[SourceDoc] | None = None) -> int:
    if target_slides:
        n = target_slides
    else:
        material = "\n".join([brief, *(source.text for source in (sources or []))])
        units = len([part for part in re.split(r"[\n.!?]+", material) if len(part.strip()) >= 20])
        n = min(8, max(MIN_SLIDES, 3 + (units + 1) // 2)) if material.strip() else _DEFAULT_TARGET_SLIDES
    return max(MIN_SLIDES, min(MAX_SLIDES, n))


def _fallback_intents(title: str, brief: str, sources: list[SourceDoc]) -> list[str]:
    """Extract concrete, source-backed topics for the deterministic outline.

    The previous fallback used internal labels such as ``Тема и цель
    презентации``.  Those labels were deliberately rejected later by the
    coverage validator, so a provider hiccup guaranteed a second failure.
    Fallback headings now come from the user's own title and materials.
    """
    material = "\n".join([brief, *(source.text for source in sources)])
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\n.!?]+", material):
        value = re.sub(r"^\s*(?:[-•–—]|\d+[.)])\s*", "", raw)
        value = " ".join(value.split()).strip(" —–-:;")
        if len(value) < 12:
            continue
        if len(value) > 96:
            shortened = value[:96].rsplit(" ", 1)[0]
            value = (shortened or value[:96]).rstrip(" ,:;")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            candidates.append(value)
    if title.strip():
        candidates.insert(0, title.strip())
    return candidates


def _fallback_outline(
    n: int, *, title: str = "", brief: str = "", sources: list[SourceDoc] | None = None,
) -> list[OutlineSlide]:
    """Детерминированный скелет структуры — используется без модели и при
    любом сбое вызова: не заглушка "пустая колода", а полноценная валидная
    структура (титул, повестка, контекст/проблема, данные, решение, кейс,
    риски, дорожная карта, итог), урезанная/растянутая до `n` штатным
    `_clamp_slide_count`."""
    topics = _fallback_intents(title, brief, sources or [])
    deck_title = title.strip() or (topics[0] if topics else "Основная тема презентации")
    middle_topics = [topic for topic in topics if topic.casefold() != deck_title.casefold()]
    if not middle_topics:
        middle_topics = [f"Ключевой аспект: {deck_title}"]
    middle_kinds = [
        "agenda", "context", "problem", "data", "solution", "how_it_works",
        "case", "risks", "roadmap", "ask",
    ]
    skeleton = [OutlineSlide(kind="title", intent=deck_title)]
    for index, kind in enumerate(middle_kinds):
        topic = middle_topics[index % len(middle_topics)]
        if index >= len(middle_topics):
            topic = f"{topic}: аспект {index + 1}"
        skeleton.append(OutlineSlide(kind=kind, intent=topic))
    skeleton.append(OutlineSlide(kind="closing", intent=f"Выводы: {deck_title}"))
    return _clamp_slide_count(skeleton, n, title=deck_title)


def _clamp_slide_count(
    slides: list[OutlineSlide], target: int, *, title: str = "",
) -> list[OutlineSlide]:
    """Гарантирует структурные инварианты AGENT.md ("первый слайд —
    титульный, последний — итоговый") и объём ТЗ (`MIN_SLIDES`..
    `MAX_SLIDES`) КОДОМ, а не доверием модели — тот же принцип, что и
    остальной проект: модель предлагает, код проверяет."""
    slides = list(slides)
    if not slides:
        slides = [OutlineSlide(kind="title", intent=title or "Основная тема презентации")]
    if slides[0].kind != "title":
        slides.insert(0, OutlineSlide(kind="title", intent=title or "Основная тема презентации"))
    if slides[-1].kind != "closing":
        slides.append(OutlineSlide(kind="closing", intent=f"Выводы: {title}" if title else "Выводы презентации"))

    requested = max(MIN_SLIDES, min(MAX_SLIDES, target))
    if len(slides) > requested:
        keep_middle = requested - 2
        slides = [slides[0], *slides[1:-1][:keep_middle], slides[-1]]

    filler_kinds = ("context", "data", "case")
    i = 0
    while len(slides) < requested:
        kind = filler_kinds[i % len(filler_kinds)]
        suffix = f": {title}" if title else " по теме"
        slides.insert(-1, OutlineSlide(kind=kind, intent=f"Дополнительные сведения{suffix}"))
        i += 1

    return slides


def _summarize_available_forms(profile) -> list[dict]:
    """Вместимость реальных раскладок ЭТОГО шаблона, по виду (`Pattern.
    kind`) — Task 18, находка №2 брифа: "модель не знает, какие формы умеет
    шаблон", структуру планировали вслепую и без оглядки на то, что шаблон
    физически может показать (цитата, крупный фактоид с подписью, ряд
    карточек). Та же величина, что `writer._kind_capacity` считает для
    ОДНОГО выбранного вида (максимум по всем паттернам вида — самая
    вместительная раскладка, не средняя и не минимум, см. её докстроку про
    происхождение этого выбора), здесь — сразу по ВСЕМ видам, которые в
    шаблоне реально есть, чтобы outline-writer видел общую картину раньше,
    чем решать, сколько текстовых пунктов подряд ставить.

    `profile is None` (без разобранного шаблона — синтетика тестов, ручной
    вызов без template) -> пустой список: структура тогда планируется, как и
    раньше этой задачи, без оглядки на форму (нечего показывать)."""
    if profile is None:
        return []
    by_kind: dict[str, list] = {}
    for p in profile.patterns:
        by_kind.setdefault(p.kind, []).append(p)
    forms = []
    for kind, patterns in sorted(by_kind.items()):
        forms.append({
            "kind": kind,
            "count": len(patterns),
            "max_items": max((p.capacity.max_items for p in patterns), default=0),
            "max_chars_per_item": max((p.capacity.max_chars_per_item for p in patterns), default=0),
        })
    return forms


def build_outline(
    brief: str,
    sources: list[SourceDoc],
    profile,
    llm: LLMProvider | None,
    target_slides: int | None = None,
    *,
    title: str = "",
    language: str = "ru",
    allow_fallback: bool = True,
) -> Outline:
    """Разбирает бриф+источники в структуру колоды. `profile` — контракт
    интерфейса брифа Task 13 дословно; сама структура (сколько слайдов, о
    чём) от шаблона напрямую не зависит (`OutlineSlide.kind` — семантическая
    тема пункта, "problem"/"data"/"case", не вид раскладки), но с Task 18
    `profile` больше не игнорируется целиком: модели передаётся сводка
    доступных ФОРМ шаблона (`_summarize_available_forms`), чтобы структура
    планировалась с оглядкой на то, что шаблон физически умеет показать
    (бриф: "если в шаблоне есть цитата и фактоид, разумно их использовать, а
    не делать двенадцать текстовых слайдов подряд") — см. правило в
    `agents/outline-writer/AGENT.md`. `profile=None` (синтетика тестов,
    вызов без разобранного шаблона) по-прежнему работает — сводка тогда
    пустая, поведение не отличается от того, что было до Task 18."""
    n = _clamp_target(target_slides, brief, sources)

    if llm is None:
        if not allow_fallback:
            raise OutlineGenerationError("модель структуры не подключена")
        reason = "модель структуры не подключена"
        return Outline(
            slides=_fallback_outline(n, title=title, brief=brief, sources=sources),
            title=title, language=language, generation_origin="fallback", generation_error=reason,
        )

    _meta, prompt_body = _load_agent_prompt()
    payload = {
        "brief": brief,
        "sources": [{"name": s.name, "text": s.text} for s in sources],
        "target_slides": n,
        "available_forms": _summarize_available_forms(profile),
    }
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    raw = ""
    try:
        raw = llm.complete(messages, schema=_SCHEMA, max_tokens=OUTLINE_MAX_TOKENS)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as parse_exc:
            repair_payload = {
                "previous_answer": raw,
                "json_error": str(parse_exc),
                "repair_instruction": (
                    "Исправь JSON по переданной схеме. Не добавляй пояснений и markdown, "
                    "верни только цельный объект JSON."
                ),
                "schema": _SCHEMA,
            }
            repair_messages = [
                {"role": "system", "content": prompt_body},
                {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)},
            ]
            repaired_raw = llm.complete(
                repair_messages, schema=_SCHEMA, max_tokens=OUTLINE_MAX_TOKENS,
            )
            data = json.loads(repaired_raw)
        raw_slides = data["slides"]
        if not isinstance(raw_slides, list):
            raise ValueError(f"'slides' должен быть списком, получено {type(raw_slides).__name__}")
        slides = []
        for item in raw_slides:
            kind = item.get("kind") if isinstance(item, dict) else None
            if kind not in OUTLINE_KINDS:
                continue  # модель предложила несуществующий тип — код отвергает, не падает
            intent = item.get("intent") or ""
            if not intent.strip():
                continue
            needs = [str(x) for x in item.get("needs", []) if isinstance(item.get("needs"), list)] \
                if isinstance(item.get("needs"), list) else []
            slides.append(OutlineSlide(kind=kind, intent=intent, needs=needs))
        if not slides:
            raise ValueError("модель не вернула ни одного валидного слайда структуры")
    except Exception as exc:
        if not allow_fallback:
            raise OutlineGenerationError(f"не удалось получить структуру презентации: {exc}") from exc
        reason = f"{type(exc).__name__}: {str(exc)[:240]}"
        logger.warning("outline generation fell back: %s", reason)
        slides = _fallback_outline(n, title=title, brief=brief, sources=sources)
        origin = "fallback"
    else:
        slides = _clamp_slide_count(slides, n, title=title)
        reason = None
        origin = "model"

    return Outline(
        slides=slides, title=title, language=language,
        generation_origin=origin, generation_error=reason,
    )


# ---------------------------------------------------------------------------
# Загрузка контент-пакета (`fixtures/content-packs/<pack>/brief.md` +
# `sources.md`) — не часть интерфейса брифа Task 13 дословно, но без неё
# нечем скормить `build_outline` реальный бриф/источники за пределами
# тестов (обязательная проверка задачи — девять презентаций по реальным
# пакетам, см. отчёт задачи).
# ---------------------------------------------------------------------------


def load_content_pack(pack_dir: Path) -> tuple[str, list[SourceDoc], dict]:
    """Читает `brief.md` (YAML-фронтматтер + тело брифа) и `sources.md`
    контент-пакета. Возвращает (текст брифа, список источников, метаданные
    фронтматтера: `title`/`purpose`/`audience`/`language`/`target_slides`)."""
    brief_path = pack_dir / "brief.md"
    sources_path = pack_dir / "sources.md"

    text = brief_path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) >= 3 and not parts[0].strip():
        meta = yaml.safe_load(parts[1]) or {}
        body = parts[2].strip()
    else:
        meta = {}
        body = text.strip()

    sources = [SourceDoc(name="sources.md", text=sources_path.read_text(encoding="utf-8"))]
    return body, sources, meta
