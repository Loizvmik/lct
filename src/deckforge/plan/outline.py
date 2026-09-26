"""Структура колоды: сколько слайдов, о чём каждый, в каком порядке —
первый шаг планирования содержания (Task 13). Раскладку каждого слайда
выбирает следующий шаг (`pattern.plan_patterns`, сразу для всей колоды),
текст пишется последним, под контракт раскладки (`writer.write_slides`);
этот модуль не знает ни о том, ни о другом, но называет, сколько единиц
содержания у пункта и нужна ли ему особая форма (`items`, `form`).

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
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from deckforge.plan.data_types import VisualIntent, visual_intents
from deckforge.plan.series import stems
from deckforge.provider.base import LLMProvider

AGENT_PATH = Path(__file__).resolve().parents[3] / "agents" / "outline-writer" / "AGENT.md"

# Закрытый список типов слайда структуры — AGENT.md, дословно.
OUTLINE_KINDS = (
    "title", "agenda", "context", "problem", "solution", "how_it_works",
    "data", "comparison", "case", "roadmap", "team", "risks", "ask", "closing",
)

# Особая форма пункта, которую планировщик раскладок сам не назначает:
# цитата и показатель только там, где материал их действительно несёт.
OUTLINE_FORMS = ("quote", "kpi", "table", "chart")

# Объём колоды — ТЗ дословно ("10-15 слайдов или заданное пользователем").
MIN_SLIDES = 10
MAX_SLIDES = 15
_DEFAULT_TARGET_SLIDES = 12

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
    # Задача P: раскладку выбирают до текста, и планировщику нужно знать,
    # сколько единиц содержания (пунктов, карточек, показателей) слайд
    # покажет и нужна ли особая форма (цитата, показатель, таблица,
    # график). Оба поля необязательны: без них число берётся из `needs`.
    items: int | None = None
    form: str | None = None
    # Задача V1: визуал пункта по типу данных источников
    # (`plan.data_types.VisualIntent`, `apply_visual_intents`): что
    # показать, обязательно ли, почему и по каким данным. Решает код до
    # письма, писатель получает данные готовыми.
    visual_intent: VisualIntent | None = None


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
                    "items": {"type": "integer"},
                    "form": {"type": "string", "enum": list(OUTLINE_FORMS)},
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


def _clamp_target(target_slides: int | None) -> int:
    n = target_slides if target_slides else _DEFAULT_TARGET_SLIDES
    return max(MIN_SLIDES, min(MAX_SLIDES, n))


def _fallback_outline(n: int) -> list[OutlineSlide]:
    """Детерминированный скелет структуры — используется без модели и при
    любом сбое вызова: не заглушка "пустая колода", а полноценная валидная
    структура (титул, повестка, контекст/проблема, данные, решение, кейс,
    риски, дорожная карта, итог), урезанная/растянутая до `n` штатным
    `_clamp_slide_count`."""
    skeleton = [
        OutlineSlide(kind="title", intent="Тема и цель презентации"),
        OutlineSlide(kind="agenda", intent="О чём пойдёт речь"),
        OutlineSlide(kind="context", intent="Контекст задачи"),
        OutlineSlide(kind="problem", intent="В чём проблема"),
        OutlineSlide(kind="data", intent="Что показывает измерение"),
        OutlineSlide(kind="solution", intent="Что предлагается сделать"),
        OutlineSlide(kind="how_it_works", intent="Как это работает"),
        OutlineSlide(kind="case", intent="Результат пилота/проверки"),
        OutlineSlide(kind="risks", intent="Риски и как их снимаем"),
        OutlineSlide(kind="roadmap", intent="Что нужно для раскатки"),
        OutlineSlide(kind="ask", intent="О чём просим комитет/аудиторию"),
        OutlineSlide(kind="closing", intent="Итог и следующий шаг"),
    ]
    return _clamp_slide_count(skeleton, n)


def _clamp_slide_count(slides: list[OutlineSlide], target: int) -> list[OutlineSlide]:
    """Гарантирует структурные инварианты AGENT.md ("первый слайд —
    титульный, последний — итоговый") и объём ТЗ (`MIN_SLIDES`..
    `MAX_SLIDES`) КОДОМ, а не доверием модели — тот же принцип, что и
    остальной проект: модель предлагает, код проверяет."""
    slides = list(slides)
    if not slides:
        slides = [OutlineSlide(kind="title", intent="Тема презентации")]
    if slides[0].kind != "title":
        slides.insert(0, OutlineSlide(kind="title", intent="Тема и цель презентации"))
    if slides[-1].kind != "closing":
        slides.append(OutlineSlide(kind="closing", intent="Итог и следующий шаг"))

    if len(slides) > MAX_SLIDES:
        keep_middle = MAX_SLIDES - 2
        slides = [slides[0], *slides[1:-1][:keep_middle], slides[-1]]

    filler_kinds = ("context", "data", "case")
    i = 0
    while len(slides) < min(target, MAX_SLIDES) or len(slides) < MIN_SLIDES:
        kind = filler_kinds[i % len(filler_kinds)]
        slides.insert(-1, OutlineSlide(kind=kind, intent="Дополнительный контекст"))
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
    n = _clamp_target(target_slides)

    if llm is None:
        return Outline(slides=apply_visual_intents(_fallback_outline(n), sources), title=title, language=language)

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

    try:
        raw = llm.complete(messages, schema=_SCHEMA, max_tokens=OUTLINE_MAX_TOKENS)
        data = json.loads(raw)
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
            items = item.get("items")
            items = items if isinstance(items, int) and not isinstance(items, bool) and items > 0 else None
            form = item.get("form") if item.get("form") in OUTLINE_FORMS else None
            slides.append(OutlineSlide(kind=kind, intent=intent, needs=needs, items=items, form=form))
        if not slides:
            raise ValueError("модель не вернула ни одного валидного слайда структуры")
    except Exception:
        slides = _fallback_outline(n)
    else:
        slides = _clamp_slide_count(slides, n)

    return Outline(slides=apply_visual_intents(slides, sources), title=title, language=language)


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


def outline_to_dict(outline: Outline) -> dict:
    """Структура для отладочного файла рядом с колодой: общая для всех
    стилей, раскладки и текст у каждого стиля свои (`<стиль>/deck.json`)."""
    return {
        "title": outline.title, "language": outline.language,
        "slides": [
            {
                "kind": s.kind, "intent": s.intent, "needs": list(s.needs), "items": s.items, "form": s.form,
                **({"visual_intent": s.visual_intent.to_dict()} if s.visual_intent is not None else {}),
            }
            for s in outline.slides
        ],
    }


# ---------------------------------------------------------------------------
# Задача V1: визуал из типа данных, до письма
# ---------------------------------------------------------------------------

_HERO_KINDS = ("title", "closing")
# Новый пункт с визуалом встаёт после пункта, с которым у набора данных
# столько общих основ слов; одной мало: «контекст» и «контента» дают
# одну основу, а о разном.
_NEAR_WORDS = 2
# Форма пункта структуры по типу визуала слоя данных.
_FORM_OF = {"chart": "chart", "table": "table", "kpi": "kpi"}


def _slide_text(slide: OutlineSlide) -> str:
    return " ".join([slide.intent, *slide.needs])


def _overlap(slide: OutlineSlide, vi: VisualIntent) -> int:
    return len(stems(_slide_text(slide)) & vi.data.words) if vi.data is not None else 0


def apply_visual_intents(slides: list[OutlineSlide], sources: list[SourceDoc]) -> list[OutlineSlide]:
    """Визуал пунктов структуры по типам данных источников
    (`plan.data_types`).

    1. Пункт, которому модель структуры сама назначила график или таблицу,
       получает ближайший по смыслу набор данных подходящего вида.
    2. Набор, для которого визуал обязателен (`VisualIntent.required`:
       динамика от четырёх точек, сравнение от трёх категорий, части
       целого), и который ни один пункт не взял, получает пункт сам: пункт
       «данные» без особой формы, пересказывающий эти числа (модель
       спланировала ряд списком), переделывается под график; иначе новый
       пункт встаёт после самого близкого по смыслу; при полной колоде
       (`MAX_SLIDES`) под график переделывается любой пункт без формы.
    Каждый набор данных достаётся одному пункту."""
    intents = visual_intents(sources)
    if not intents or not slides:
        return slides
    slides = list(slides)
    used: set[str] = set()

    for i, slide in enumerate(slides):
        if slide.form not in ("chart", "table") or slide.visual_intent is not None:
            continue
        pool = [
            vi for vi in intents
            if vi.data_ref not in used and (vi.type == "chart" if slide.form == "chart" else vi.type in ("table", "chart"))
        ]
        best = _best(slide, pool)
        if best is None:
            continue
        if slide.form == "table" and best.type == "chart":
            best = VisualIntent(
                type="table", required=False, data_ref=best.data_ref, data=best.data,
                reason="структура заказала таблицу по этим числам",
            )
        slides[i] = replace(slide, visual_intent=best)
        used.add(best.data_ref)

    for vi in intents:
        if not vi.required or vi.data_ref in used:
            continue
        content = [i for i, s in enumerate(slides) if s.kind not in _HERO_KINDS and s.visual_intent is None]
        free = [i for i in content if slides[i].form is None]
        retold = [i for i in free if slides[i].kind == "data" and _overlap(slides[i], vi) > 0]
        if retold:
            target = max(retold, key=lambda i: (_overlap(slides[i], vi), -i))
            slides[target] = replace(slides[target], form=_FORM_OF[vi.type], items=1, visual_intent=vi)
        elif len(slides) < MAX_SLIDES:
            near = max(content, key=lambda i: (_overlap(slides[i], vi), -i), default=None)
            at = near + 1 if near is not None and _overlap(slides[near], vi) >= _NEAR_WORDS else max(1, len(slides) - 1)
            slides.insert(at, _new_slide(vi))
        elif free:
            slides[free[0]] = replace(slides[free[0]], form=_FORM_OF[vi.type], items=1, visual_intent=vi)
        else:
            continue
        used.add(vi.data_ref)
    return slides


def _best(slide: OutlineSlide, pool: list[VisualIntent]) -> VisualIntent | None:
    """Ближайший по словам набор; при равенстве первый по источнику. Без
    единого общего слова первый обязательный, потом первый вообще: модель
    заказала график, и числа у неё из тех же источников."""
    if not pool:
        return None
    return max(enumerate(pool), key=lambda p: (_overlap(slide, p[1]), p[1].required, -p[0]))[1]


def _new_slide(vi: VisualIntent) -> OutlineSlide:
    ds = vi.data
    what = ds.table.heading if ds is not None and ds.table.heading else "данные источника"
    names = ", ".join(s.name for s in ds.series.series) if ds is not None and ds.series is not None else ""
    return OutlineSlide(
        kind="data",
        intent=f"Вывод из чисел раздела «{what}»" + (f": {names}" if names else ""),
        needs=[f"{vi.reason}; вывод из ряда {names or what}"],
        items=1, form=_FORM_OF[vi.type], visual_intent=vi,
    )
