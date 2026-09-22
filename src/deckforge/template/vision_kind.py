"""Уточняет `Pattern.kind` мультимодальной моделью по отрисованной картинке
слайда-примера — task-18-brief, находка №1: геометрический классификатор
(`template/patterns.py::_classify_kind`) различает только семь корзин форм
(повтор/колонки/площадь картинки), без понимания СМЫСЛА композиции. Слайд с
цитатой, слайд-фактоид с крупной подписью, слайд с фото/мокапом устройства
рядом с текстом — геометрически неотличимы от обычного текстового слайда
(один крупный текстовый блок) и схлопываются в `section`/`bullets`; ручной
разбор задачи (VK Education) — 22 из 33 намайненных раскладок ушли в эти два
вида.

**Вторая (и единственная кроме неё) точка пакета `template/`, которая реально
зовёт модель** — намеренное расширение архитектурной границы, заявленной
`template/patterns.py` ("модуль полностью детерминированный... не обращается
к модели") и `template/naming.py` ("единственный модуль пакета, который
реально ВЫЗЫВАЕТ модель"): та докстрока была верна до этой задачи, дальше
неверна буквально, поэтому изменена явно, а не молча — граница теперь "не
`patterns.py` сам, а отдельный модуль поверх уже намайненных им паттернов",
и это закреплено тестом (`tests/template/test_architecture.py`). Основание
расширения — критерий оценки конкурса прямо называет «CV/LLM-оркестрацию»
как часть решения, не только детерминированный разбор OOXML.

Тот же принцип, что и у `naming.py`/`layouts.py` (словарь синонимов имён
макетов, `config/layout-kinds.yaml`):

- **модель предлагает, код проверяет** — `id`, которого нет в
  `config/pattern-kinds.yaml`, отвергается, геометрический `kind` остаётся
  как снял `patterns.py`;
- **без ключа модели всё работает по-старому** — `llm=None` вообще не
  трогает `patterns`, ни одного сетевого вызова, ни одной операции рендера;
- **список видов — конфиг, не код** (`config/pattern-kinds.yaml`) — расширить
  словарь видов (например, добавить "диаграмма процесса") значит править
  только этот YAML, не код классификатора;
- **честная деградация** — рендер шаблона (`render.soffice.to_pngs`) не
  удался, модель не ответила валидным JSON, предложила вид вне списка —
  каждый из этих случаев не роняет сборку профиля, а оставляет геометрический
  `kind` этого паттерна как есть и попадает в возвращаемые заметки
  (`TemplateProfile.provenance`/`.warnings` читают их так же, как заметки
  `naming.name_palette_roles_report`).

Результат кешируется ВМЕСТЕ с остальным профилем (`TemplateProfile.
from_file`, диск-кеш по отпечатку файла) — обращения к модели не повторяются
на каждую генерацию колоды того же шаблона, только на первый разбор
незнакомого файла."""
from __future__ import annotations
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

import yaml

from deckforge.provider.base import VisionProvider
from deckforge.render.soffice import RenderError, to_pngs
from deckforge.template.patterns import Pattern

AGENT_PATH = Path(__file__).resolve().parents[3] / "agents" / "pattern-kind-vision" / "AGENT.md"
KINDS_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "pattern-kinds.yaml"

# Сколько раскладок классифицируются ОДНОВРЕМЕННО — тот же порядок величины
# и то же рассуждение, что у `plan.writer.DEFAULT_WRITER_MAX_WORKERS`/
# `audit.visual.run_visual(max_workers=4)`: одна и та же модель (Yandex
# Cloud) за одним и тем же провайдером, не "чем больше, тем быстрее" — запас
# на сетевые ретраи каждого параллельного вызова важнее скорости самого
# первого разбора (который к тому же кешируется и оплачивается один раз на
# файл, не на каждую генерацию).
DEFAULT_MAX_WORKERS = 4

# Ответ короткий (один JSON-объект `{"kind": "..."}`), но qwen3.6 —
# рассуждающая модель, и почти весь бюджет уходит в `reasoning_content`
# независимо от длины финального ответа (та же находка, что уже задокумент-
# ирована `naming._PALETTE_NAMING_INITIAL_MAX_TOKENS`/`outline.OUTLINE_MAX_
# TOKENS`/`writer.WRITER_MAX_TOKENS` — короткий ответ не значит маленький
# бюджет для ЭТОЙ модели). Живой прогон обязательной проверки задачи
# (VK Education, 33 паттерна): старт с 512 — 22 из 33 вызовов ушли в
# эскалацию, 15 не дожали ответ даже к потолку `MAX_BUDGET_ESCALATIONS`=2
# (512→1024→2048, `provider/yandex.py`) и остались на геометрическом виде.
# 3072 — на класс задачи меньше `WRITER_MAX_TOKENS`/`OUTLINE_MAX_TOKENS`
# (6144, там модель пишет/планирует контент, а не выбирает id из готового
# списка), но заметно выше 512: одна эскалация (3072→6144, упирается в
# потолок `MAX_TOKENS_BUDGET_CAP`) отводит ответу ДО 6144 токенов реасонинга
# суммарно за один вызов, а не три — честно объявленная калибровочная
# величина (не переизмерена повторно на этой правке), не окончательное
# число: если доля неответивших паттернов останется высокой и на 3072,
# порог стоит поднять ещё раз, а не считать 22/33 нормой.
_MAX_TOKENS = 3072


def _load_agent_prompt(path: Path = AGENT_PATH) -> str:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path}: ожидался YAML-фронтматтер, ограниченный `---`")
    return parts[2].strip()


def load_pattern_kinds(path: Path = KINDS_CONFIG_PATH) -> list[dict]:
    """Словарь видов раскладки — `[{"id": "quote", "description": "..."}, ...]`,
    ровно как лежит в `config/pattern-kinds.yaml` (`kinds:`), без обработки:
    и код (валидация ответа модели), и промпт (список, который видит модель)
    читают ОДИН и тот же список, а не две копии, которым предстоит разъехаться."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kinds = data.get("kinds") or []
    return [{"id": k["id"], "description": k.get("description", "")} for k in kinds]


def allowed_kind_ids(path: Path = KINDS_CONFIG_PATH) -> frozenset[str]:
    return frozenset(k["id"] for k in load_pattern_kinds(path))


def classify_patterns_by_vision(
    patterns: list[Pattern],
    template_path: Path,
    llm: VisionProvider | None,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> tuple[list[Pattern], list[str]]:
    """Уточняет `Pattern.kind` каждого паттерна показом модели картинки того
    слайда-примера, с которого он снят (`Pattern.source_slide_index[0]`, тот
    же слайд, что и обоснование самого паттерна в отчёте — первый источник,
    когда паттернов, снятых с разных, но геометрически совпавших слайдов,
    несколько, см. `patterns._dedup`).

    Возвращает (новый список паттернов, заметки для `TemplateProfile.
    provenance`/`.warnings`) — НИКОГДА не бросает исключение наружу: сбой
    рендера, сети или разбора ответа модели превращает соответствующие
    паттерны в "оставлены геометрическим предположением", не в упавший
    разбор профиля (тот же принцип честной деградации, что у
    `naming.name_palette_roles_report`)."""
    if llm is None or not patterns:
        return list(patterns), []

    kinds = load_pattern_kinds()
    if not kinds:
        return list(patterns), [
            "Виды раскладки моделью не уточнялись: config/pattern-kinds.yaml пуст или не читается."
        ]
    allowed = frozenset(k["id"] for k in kinds)

    try:
        agent_body = _load_agent_prompt()
    except (OSError, ValueError) as exc:
        return list(patterns), [f"Виды раскладки моделью не уточнялись: промпт не загрузился ({exc})."]

    with tempfile.TemporaryDirectory(prefix="deckforge-vision-kind-") as tmp_dir:
        try:
            pngs = to_pngs(Path(template_path), Path(tmp_dir))
        except RenderError as exc:
            return list(patterns), [f"Виды раскладки моделью не уточнялись: рендер шаблона не удался ({exc})."]
        # `to_pngs` рендерит ЦЕЛИКОМ тот же файл, с которого `mine_patterns`
        # снял паттерны, — страница N PDF/PNG это слайд N исходного .pptx
        # без пропусков, поэтому 1-based номер страницы (порядок списка,
        # `to_pngs` уже сортирует его по номеру страницы, см. её докстроку)
        # совпадает с `Pattern.source_slide_index` буквально, без отдельного
        # сопоставления имён файлов.
        png_by_slide = {i + 1: png.read_bytes() for i, png in enumerate(pngs)}

        reclassified: dict[str, str] = {}  # pattern_id -> новый kind
        skip_notes: list[str] = []

        def _ask_one(pattern: Pattern) -> tuple[str, str | None, str | None]:
            """Возвращает (pattern_id, новый_kind_или_None, заметка_об_отказе_или_None)."""
            slide_no = pattern.source_slide_index[0] if pattern.source_slide_index else None
            png = png_by_slide.get(slide_no) if slide_no is not None else None
            if png is None:
                return pattern.pattern_id, None, (
                    f"{pattern.pattern_id}: слайд-источник {slide_no} не нашёлся среди отрисованных "
                    f"страниц — вид оставлен геометрическим ({pattern.kind})."
                )
            payload = {
                "kinds": kinds,
                "geometric_hint": pattern.kind,
            }
            prompt = f"{agent_body}\n\nСлужебные данные:\n{json.dumps(payload, ensure_ascii=False)}"
            try:
                raw = llm.ask_image(png, prompt, max_tokens=_MAX_TOKENS)
                proposed = json.loads(raw).get("kind")
            except Exception as exc:  # сеть/парсинг — не должны ронять разбор профиля
                return pattern.pattern_id, None, (
                    f"{pattern.pattern_id}: вид моделью не получен ({exc}) — оставлен "
                    f"геометрическим ({pattern.kind})."
                )
            if not isinstance(proposed, str) or proposed not in allowed:
                return pattern.pattern_id, None, (
                    f"{pattern.pattern_id}: модель предложила вид {proposed!r}, которого нет в "
                    f"config/pattern-kinds.yaml — оставлен геометрическим ({pattern.kind})."
                )
            return pattern.pattern_id, proposed, None

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = [pool.submit(_ask_one, p) for p in patterns]
            for future in as_completed(futures):
                pattern_id, new_kind, note = future.result()
                if new_kind is not None:
                    reclassified[pattern_id] = new_kind
                if note is not None:
                    skip_notes.append(note)

    changed = sum(1 for p in patterns if reclassified.get(p.pattern_id) not in (None, p.kind))
    result = [
        replace(p, kind=reclassified[p.pattern_id]) if p.pattern_id in reclassified else p
        for p in patterns
    ]

    notes = [
        f"Вид раскладки уточнён моделью у {changed} из {len(patterns)} паттернов "
        f"(показана картинка слайда-примера, см. agents/pattern-kind-vision/AGENT.md)."
    ]
    notes.extend(sorted(skip_notes))
    return result, notes
