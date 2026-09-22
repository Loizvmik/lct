# DeckForge — цифровой дизайнер презентаций. План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сервис, который разбирает произвольный .pptx-шаблон на дизайн-систему и композиционные паттерны, собирает по краткому брифу презентацию из 10–15 слайдов в трёх вариантах вёрстки, проверяет результат детерминированным и модельным аудитом и выгружает в .pptx нативными объектами, .pdf и .html.

**Architecture:** Три слоя с жёсткой границей. Первый — детерминированный разбор OOXML в `TemplateProfile` (токены, каталог лейаутов, каталог паттернов, каталог ассетов); он не зовёт модель вообще. Второй — планирование содержания моделью: бриф и контент-пакет превращаются в `DeckSpec`, где нет ни одной координаты и ни одного цвета. Третий — детерминированная вёрстка: код кладёт `DeckSpec` в паттерны шаблона поверх его же лейаутов и прогоняет аудит. Модель пишет только текст и выбирает паттерн из предложенного кодом списка; всё остальное — код по профилю шаблона. Агентную обвязку (цикл, tools, skills, версионирование) даёт форк Mini-Agent, MIT.

**Tech Stack:** Python 3.12, python-pptx + lxml (прямой XML там, где python-pptx слеп), Pillow (замер текста), pydantic v2, FastAPI, Next.js 15 + React 19 + TypeScript, LibreOffice headless (рендер PNG/PDF), pytest. LLM/VLM — `qwen3.6-35b-a3b` через OpenAI-совместимый эндпоинт Yandex AI Studio.

## Global Constraints

Значения скопированы из ТЗ «4. VK Tech.pdf» дословно; каждая задача плана обязана им соответствовать.

- Генеративные модели: **только с открытыми весами и лицензией Apache 2.0 / MIT. LLM\VLM\text-to-image до 35B.** Единственная модель, удовлетворяющая этому на доступном ключе, — `qwen3.6-35b-a3b` (Apache 2.0, 35B MoE, мультимодальная). Запасная — `gpt-oss-20b` (Apache 2.0, 21B, без vision). `gpt-oss-120b`, `qwen3-235b-a22b-fp8`, `yandexgpt-*`, `aliceai-*`, `deepseek-v4-flash` использовать запрещено.
- **Целевой объём 10–15 слайдов (или заданный пользователем).**
- **Время генерации одной колоды — не более 5 минут.**
- **Решение не должно быть заточено под три предоставленных шаблона: на финальной защите презентации генерируются по шаблону, который команды не видели ранее.**
- **Экспорт в .html, .pptx, .pdf. В .pptx слайды выгружаются нативными объектами. Слайд, выгруженный единым растровым изображением, не засчитывается.**
- **Представление результата генерации в трёх вариантах вёрстки для одного и того же шаблона и контента. Варианты должны быть визуально различимы и при этом одинаково соответствовать правилам шаблона.**
- **Аудит — часть пайплайна, а не внешняя проверка.** Проверки делятся на детерминированные и недетерминированные.
- **Промпты\конфиги скиллов и агентов в репозитории лежат отдельными файлами, т.е. не зашиты в код.**
- **Версионирование скиллов и агентов, лежащих в основе воркфлоу.**
- Тип устройств: **только десктоп**. Веб-интерфейс работает в актуальной и предыдущей версиях Chrome, Firefox, Safari, Яндекс Браузер на MacOS и Windows.
- Бэкенд и фронтенд: Python/Typescript; React, Next.js, Vue или Streamlit/Gradio.
- Документация в репозитории: **README** (сетап, переменные окружения, ограничения), **ARCHITECTURE** (пайплайн, границы слоёв: парсинг, генерация, вёрстка, аудит, экспорт), **MODELS** (используемые модели, области применения, системные требования\ссылки на huggingface), **AUDIT** (список тестов и их область покрытия).
- Воспроизводимый сетап и запуск конфиг-файлом.

## Что выяснила разведка и что из этого следует

Четыре шаблона в `dataset/templates/` — экспорт из Google Slides. Проверено: все шейпы названы `Google Shape;<N>;p<M>`, `docProps/` отсутствует, у всех частей одинаковый timestamp. Последствия жёстко определяют парсер:

1. **`p:txStyles` в мастерах — заглушка** во всех файлах: titleStyle, bodyStyle, otherStyle, все девять уровней, `sz=1400 b=0 latin=Arial clr=#000000 lnSpc=100%`. Типографику из мастера брать нельзя.
2. **`a:fontScheme` — заглушка**: `name="Office"`, majorFont = minorFont = `Arial`. Реальный шрифт бренда (`Play`) виден только в run-level `a:latin` и в `p:embeddedFontLst`.
3. **`p:sldLayout/@type` = None у всех 84 лейаутов.** Семантики типа лейаута в XML нет.
4. **`p:guideLst` отсутствует** везде. Сетку восстанавливать кластеризацией координат.
5. **Тема не по имени файла.** У VK WorkSpace `theme1.xml` — дефолтная офисная заглушка, брендовая палитра в `theme2`, привязанном к master1 через rels. Наивное чтение `ppt/theme/theme1.xml` выдаст палитру Microsoft и не заметит.
6. **`dk1` используется как цвет фона.** У VK Tech 23 из 39 лейаутов имеют `p:bg = solidFill schemeClr dk1` (чёрный), текст заголовка `lt1`. Эвристика «dk1 — текст, lt1 — фон» даст инверсию.
7. **К плейсхолдерам привязано 4.8% / 11.2% / 24.1% шейпов.** Подстановка контента в плейсхолдеры даёт пустые слайды. Отсюда майнинг паттернов.
8. **python-pptx не применяет аффинное преобразование групп.** `child.left` возвращает сырую координату в системе координат группы. Без резолва позиции содержимого 6/28/19 групп будут мусором молча, без исключения.
9. Ни `ppt/charts/`, ни `ppt/diagrams/`, ни OLE ни в одном файле нет. Графики и SmartArt-подобные блоки создаются с нуля по токенам шаблона.
10. Холст VK Tech — 10×5.625″, у остальных 13.333×7.5″. Сравнение кеглей между шаблонами требует нормировки `sz × (12192000 / slide_width)`.

## File Structure

```
lct_ex_4/
├── config/
│   ├── app.yaml                    # единственная точка запуска (ТЗ: воспроизводимый запуск конфиг-файлом)
│   └── models.yaml                 # реестр моделей: id, лицензия, размер, роль
├── agents/                         # промпты агентов, отдельными файлами (ТЗ п.4)
│   ├── outline-writer/AGENT.md
│   ├── slide-writer/AGENT.md
│   ├── pattern-picker/AGENT.md
│   ├── palette-namer/AGENT.md
│   └── content-auditor/AGENT.md
├── skills/                         # SKILL.md в формате Mini-Agent, версионируются
│   ├── template-decompose/SKILL.md
│   ├── deck-compose/SKILL.md
│   └── deck-audit/SKILL.md
├── vendor/mini_agent/              # форк MiniMax-AI/Mini-Agent (MIT)
├── src/deckforge/
│   ├── __init__.py
│   ├── settings.py                 # загрузка config/app.yaml + .env
│   ├── provider/
│   │   ├── base.py                 # LLMProvider, VisionProvider — интерфейсы
│   │   ├── yandex.py               # OpenAI-совместимый клиент Yandex AI Studio
│   │   └── registry.py             # проверка лицензии и размера модели по config/models.yaml
│   ├── ooxml/                      # низкоуровневый доступ, ничего не знает о презентациях
│   │   ├── package.py              # zip + rels + поиск партов
│   │   ├── ns.py                   # namespace-константы
│   │   ├── color.py                # DrawingML: srgbClr/schemeClr/alpha/lumMod/shade/tint
│   │   └── geometry.py             # EMU, Box в долях, аффинный резолв групп
│   ├── template/
│   │   ├── theme.py                # clrScheme + clrMap + детект деградации
│   │   ├── usage.py                # гистограммы фактических цветов и шрифтов
│   │   ├── typography.py           # шкала кеглей, межстрочные, выравнивание
│   │   ├── grid.py                 # поля, колонки, вертикальные якоря
│   │   ├── layouts.py              # каталог лейаутов + классификация типа
│   │   ├── assets.py               # media → logo / background / icon / photo
│   │   ├── patterns.py             # майнинг композиционных паттернов
│   │   ├── naming.py               # LLM: частотный цвет → семантическая роль
│   │   └── profile.py              # TemplateProfile, сборка, сериализация, provenance
│   ├── plan/
│   │   ├── spec.py                 # DeckSpec / SlideSpec / блоки контента
│   │   ├── outline.py              # бриф + контент-пакет → структура колоды
│   │   ├── writer.py               # структура → текст слайдов
│   │   └── variants.py             # три оси различия вариантов вёрстки
│   ├── compose/
│   │   ├── builder.py              # DeckSpec + TemplateProfile + вариант → .pptx
│   │   ├── textfit.py              # замер текста Pillow, общий со слоем аудита
│   │   ├── blocks.py               # текст, буллеты, карточки, фактоиды, цитата
│   │   ├── charts.py               # нативные графики PowerPoint
│   │   ├── tables.py               # нативные таблицы
│   │   ├── diagrams.py             # SmartArt-подобные блоки из шейпов
│   │   └── decor.py                # перенос декора паттерна и ассетов шаблона
│   ├── audit/
│   │   ├── findings.py             # Finding, severity, категории, id проверок
│   │   ├── deterministic.py        # вёрстка / шаблон / плотность / целостность
│   │   ├── visual.py               # 11 вопросов по PNG слайда через VLM
│   │   ├── fixers.py               # применение выбранных пользователем исправлений
│   │   └── report.py               # сводный отчёт с координатами проблем
│   ├── render/
│   │   └── soffice.py              # pptx → pdf, pptx → png, профиль на вызов
│   ├── export/
│   │   ├── html.py                 # интерактивный HTML-дек
│   │   └── bundle.py               # pptx + pdf + html одним архивом
│   ├── api/
│   │   ├── app.py                  # FastAPI
│   │   ├── jobs.py                 # фоновые задачи, прогресс
│   │   └── schemas.py
│   └── cli.py                      # deckforge parse|generate|audit|export
├── web/                            # Next.js 15
├── fixtures/
│   ├── content-packs/              # тестовые контент-пакеты
│   └── expected/                   # золотые TemplateProfile для регрессии
├── tests/
├── docs/{README,ARCHITECTURE,MODELS,AUDIT}.md
└── config.example.yaml
```

Граница слоёв, которую нельзя нарушать (её защищает тест в Task 17): `src/deckforge/ooxml/` и `src/deckforge/template/` не импортируют `provider` нигде, кроме `naming.py`; `src/deckforge/compose/` не импортирует `provider` вообще; `src/deckforge/plan/` не импортирует `python-pptx`.

---

### Task 1: Скелет репозитория, конфиг, провайдер модели

**Files:**
- Create: `pyproject.toml`, `config/app.yaml`, `config/models.yaml`, `config.example.yaml`
- Create: `src/deckforge/__init__.py`, `src/deckforge/settings.py`
- Create: `src/deckforge/provider/base.py`, `src/deckforge/provider/yandex.py`, `src/deckforge/provider/registry.py`
- Test: `tests/test_settings.py`, `tests/test_provider_registry.py`, `tests/test_provider_yandex.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `Settings.load(path: Path) -> Settings`; `LLMProvider.complete(messages: list[Msg], *, schema: dict | None = None, max_tokens: int = 4096, temperature: float = 0.3) -> str`; `VisionProvider.ask_image(png: bytes, prompt: str, *, max_tokens: int = 1024) -> str`; `YandexProvider(model: str, api_key: str, folder_id: str)` реализует оба; `registry.assert_allowed(model_id: str) -> ModelCard` бросает `ModelNotAllowed`.

- [ ] **Step 1: Написать падающий тест реестра моделей**

```python
# tests/test_provider_registry.py
import pytest
from deckforge.provider.registry import assert_allowed, ModelNotAllowed

def test_qwen35b_is_allowed():
    card = assert_allowed("qwen3.6-35b-a3b")
    assert card.license == "Apache-2.0"
    assert card.params_b <= 35
    assert card.vision is True

def test_model_over_35b_is_rejected():
    with pytest.raises(ModelNotAllowed, match="35B"):
        assert_allowed("gpt-oss-120b")

def test_proprietary_model_is_rejected():
    with pytest.raises(ModelNotAllowed, match="лицензи"):
        assert_allowed("yandexgpt-5-pro")

def test_unknown_model_is_rejected():
    with pytest.raises(ModelNotAllowed, match="не описана"):
        assert_allowed("some-model-nobody-declared")
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `uv run pytest tests/test_provider_registry.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'deckforge.provider'`

- [ ] **Step 3: Написать `config/models.yaml`**

Реестр — данные, не код: ТЗ требует раздел MODELS с лицензиями и ссылками на huggingface, и он должен собираться из одного источника.

```yaml
models:
  - id: qwen3.6-35b-a3b
    provider: yandex
    hf: Qwen/Qwen3.6-35B-A3B
    license: Apache-2.0
    params_b: 35
    active_params_b: 3
    vision: true
    roles: [outline, writer, pattern_picker, palette_namer, content_audit]
  - id: gpt-oss-20b
    provider: yandex
    hf: openai/gpt-oss-20b
    license: Apache-2.0
    params_b: 21
    vision: false
    roles: [outline, writer, pattern_picker, palette_namer]
denied:
  - id: gpt-oss-120b
    reason: "120B превышает потолок 35B из ТЗ"
  - id: qwen3-235b-a22b-fp8
    reason: "235B превышает потолок 35B из ТЗ"
  - id: yandexgpt-5-pro
    reason: "закрытая лицензия, ТЗ требует Apache 2.0 / MIT"
  - id: yandexgpt-5-lite
    reason: "закрытая лицензия, ТЗ требует Apache 2.0 / MIT"
  - id: aliceai-llm
    reason: "закрытая лицензия, ТЗ требует Apache 2.0 / MIT"
  - id: deepseek-v4-flash
    reason: "закрытая лицензия, ТЗ требует Apache 2.0 / MIT"
```

- [ ] **Step 4: Реализовать `registry.py`**

```python
# src/deckforge/provider/registry.py
"""Проверка модели на соответствие ТЗ до первого запроса, а не после счёта."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import yaml
from pydantic import BaseModel

ALLOWED_LICENSES = {"Apache-2.0", "MIT"}
MAX_PARAMS_B = 35
REGISTRY_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"


class ModelNotAllowed(RuntimeError):
    pass


class ModelCard(BaseModel):
    id: str
    provider: str
    hf: str
    license: str
    params_b: float
    active_params_b: float | None = None
    vision: bool = False
    roles: list[str] = []


@lru_cache(maxsize=1)
def _registry(path: str = str(REGISTRY_PATH)) -> tuple[dict[str, ModelCard], dict[str, str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    allowed = {m["id"]: ModelCard(**m) for m in data.get("models", [])}
    denied = {d["id"]: d["reason"] for d in data.get("denied", [])}
    return allowed, denied


def assert_allowed(model_id: str) -> ModelCard:
    allowed, denied = _registry()
    if model_id in denied:
        raise ModelNotAllowed(f"{model_id}: {denied[model_id]}")
    card = allowed.get(model_id)
    if card is None:
        raise ModelNotAllowed(
            f"{model_id} не описана в config/models.yaml. "
            "ТЗ требует перечислить лицензию и размер каждой используемой модели."
        )
    if card.license not in ALLOWED_LICENSES:
        raise ModelNotAllowed(f"{model_id}: лицензия {card.license}, ТЗ требует Apache 2.0 или MIT")
    if card.params_b > MAX_PARAMS_B:
        raise ModelNotAllowed(f"{model_id}: {card.params_b}B превышает потолок 35B из ТЗ")
    return card
```

- [ ] **Step 5: Прогнать тест реестра**

Run: `uv run pytest tests/test_provider_registry.py -v`
Expected: 4 passed

- [ ] **Step 6: Написать тест провайдера с живым вызовом**

Тест помечается `@pytest.mark.live` и пропускается без ключа — иначе CI у экспертов упадёт на чужом окружении.

```python
# tests/test_provider_yandex.py
import json, os, pytest
from deckforge.provider.yandex import YandexProvider

live = pytest.mark.skipif(not os.getenv("YANDEX_API_KEY"), reason="нет YANDEX_API_KEY")

@pytest.fixture
def provider():
    return YandexProvider(
        model="qwen3.6-35b-a3b",
        api_key=os.environ["YANDEX_API_KEY"],
        folder_id=os.environ["YANDEX_FOLDER_ID"],
    )

@live
def test_complete_returns_json_matching_schema(provider):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    out = provider.complete(
        [{"role": "user", "content": "Столица Франции. Ответь JSON с полем city."}],
        schema=schema, max_tokens=300,
    )
    assert json.loads(out)["city"].lower().startswith("париж")

@live
def test_reasoning_content_is_not_mistaken_for_answer(provider):
    """qwen3.6 кладёт размышление в reasoning_content, а content бывает пустым.
    Клиент обязан дождаться непустого content, а не вернуть размышление."""
    out = provider.complete([{"role": "user", "content": "Скажи ровно: ОК"}], max_tokens=600)
    assert "ОК" in out
    assert "user" not in out.lower()

@live
def test_vision_reads_the_image(provider, tmp_path):
    from PIL import Image
    p = tmp_path / "blue.png"
    Image.new("RGB", (64, 64), (0, 0, 255)).save(p)
    answer = provider.ask_image(p.read_bytes(), "Одним словом: какого цвета изображение?")
    assert "син" in answer.lower()

def test_provider_refuses_disallowed_model():
    from deckforge.provider.registry import ModelNotAllowed
    with pytest.raises(ModelNotAllowed):
        YandexProvider(model="gpt-oss-120b", api_key="x", folder_id="y")
```

- [ ] **Step 7: Реализовать `base.py` и `yandex.py`**

Две вещи, на которых клиент ломается, и обе проверены живыми запросами: при малом `max_tokens` модель тратит бюджет на `reasoning_content` и отдаёт `content: null`; ответ приходит с ведущими `\n\n`.

```python
# src/deckforge/provider/yandex.py
from __future__ import annotations
import base64, json, time
from typing import Any
import httpx
from .base import LLMProvider, VisionProvider, Msg
from .registry import assert_allowed

ENDPOINT = "https://llm.api.cloud.yandex.net/v1/chat/completions"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class YandexProvider(LLMProvider, VisionProvider):
    """OpenAI-совместимый клиент Yandex AI Studio.

    Модель проверяется реестром в конструкторе: запрос к модели вне ТЗ
    не должен уйти в сеть ни разу.
    """

    def __init__(self, model: str, api_key: str, folder_id: str, *, timeout: float = 180.0):
        self.card = assert_allowed(model)
        self.model_uri = f"gpt://{folder_id}/{model}/latest"
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        )

    def complete(self, messages: list[Msg], *, schema: dict | None = None,
                 max_tokens: int = 4096, temperature: float = 0.3) -> str:
        if schema is not None:
            messages = [*messages, {
                "role": "system",
                "content": "Ответь одним объектом JSON по схеме, без markdown-ограды:\n"
                           + json.dumps(schema, ensure_ascii=False),
            }]
        body: dict[str, Any] = {
            "model": self.model_uri, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        return self._extract(self._post(body))

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        if not self.card.vision:
            raise RuntimeError(f"{self.card.id} не мультимодальна, vision-аудит ей недоступен")
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        return self._extract(self._post({
            "model": self.model_uri, "max_tokens": max_tokens, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
        }))

    def _post(self, body: dict, attempts: int = 4) -> dict:
        delay = 1.0
        for attempt in range(attempts):
            response = self._client.post(ENDPOINT, json=body)
            if response.status_code in _RETRY_STATUS and attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("недостижимо")

    @staticmethod
    def _extract(payload: dict) -> str:
        message = payload["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        if content:
            return content
        # content пуст, когда весь бюджет ушёл в reasoning_content: это не ответ,
        # а обрезанное размышление, и отдавать его наверх нельзя.
        raise RuntimeError(
            "модель не вернула ответ: весь бюджет токенов ушёл на reasoning_content, "
            f"finish_reason={payload['choices'][0].get('finish_reason')}. Увеличьте max_tokens."
        )
```

- [ ] **Step 8: Прогнать все тесты провайдера с ключом**

Run: `set -a && . ./.env && set +a && uv run pytest tests/test_provider_yandex.py -v`
Expected: 4 passed

- [ ] **Step 9: Коммит**

```bash
git add pyproject.toml config src/deckforge tests
git commit -m "feat(provider): реестр моделей по ТЗ и клиент Yandex AI Studio"
```

---

### Task 2: Низкоуровневый OOXML — пакет, цвет, геометрия групп

Самая коварная часть. Разведка нашла здесь три бага, каждый из которых портит результат молча: координаты детей групп в локальной системе, тема не по имени файла, цветовые модификаторы, которых python-pptx не отдаёт.

**Files:**
- Create: `src/deckforge/ooxml/ns.py`, `package.py`, `color.py`, `geometry.py`
- Test: `tests/ooxml/test_package.py`, `test_color.py`, `test_geometry.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `PptxPackage.open(path: Path) -> PptxPackage`; `.part(name: str) -> bytes`; `.xml(name: str) -> lxml.etree._Element`; `.rels(part_name: str) -> dict[str, str]` (rId → имя парта); `.related(part_name: str, rel_type_suffix: str) -> list[str]`; `.names() -> list[str]`; `.media() -> list[MediaEntry]`.
  - `resolve_color(node, scheme: dict[str, str], clr_map: dict[str, str]) -> Color | None`, где `Color(hex: str, alpha: float)`.
  - `Box(left: float, top: float, width: float, height: float)` в долях холста, методы `.right`, `.bottom`, `.area`, `.intersect(other) -> Box | None`.
  - `shape_box(element, canvas: Canvas, chain: Sequence[GroupFrame]) -> Box | None` — применяет аффинное преобразование всех групп по цепочке; `None`, когда у шейпа нет `a:xfrm` и координаты наследуются от плейсхолдера лейаута.
  - `walk_shapes(tree_root, canvas, *, include_groups: bool = False) -> Iterator[ShapeRef]` — рекурсивный обход `p:spTree` слайда, лейаута или мастера в документном порядке (z-order снизу вверх), со спуском в `p:grpSp` и накоплением цепочки групп.
  - `ShapeRef(element, kind, box: Box | None, name, shape_id, rotation, flip_h, flip_v, group_depth, group_chain, is_placeholder, ph_type: str | None, ph_idx: int | None)`; `kind` ∈ `{"shape","picture","graphic_frame","connector","group"}`. Отсутствие `p:ph/@type` в OOXML означает тип `body`, а не «типа нет» — `ph_type` возвращает `"body"`.

- [ ] **Step 1: Тест на аффинное преобразование групп**

Это тест-страж главного бага. Числа взяты из реального шаблона VK Education, где группа имеет `off=(282380, 3403462)`, `ext=(1195387, 99124)`, `chOff=(658813, 5548708)`, `chExt=(2283170, 189326)`.

```python
# tests/ooxml/test_geometry.py
from deckforge.ooxml.geometry import Canvas, GroupFrame, resolve_point

CANVAS = Canvas(width_emu=12192000, height_emu=6858000)

def test_child_of_group_is_mapped_into_slide_coordinates():
    frame = GroupFrame(off=(282380, 3403462), ext=(1195387, 99124),
                       ch_off=(658813, 5548708), ch_ext=(2283170, 189326))
    # ребёнок стоит ровно в начале детской системы координат
    x, y = resolve_point(658813, 5548708, [frame])
    assert (x, y) == (282380, 3403462)

def test_child_at_far_corner_of_group_lands_on_group_edge():
    frame = GroupFrame(off=(282380, 3403462), ext=(1195387, 99124),
                       ch_off=(658813, 5548708), ch_ext=(2283170, 189326))
    x, y = resolve_point(658813 + 2283170, 5548708 + 189326, [frame])
    assert abs(x - (282380 + 1195387)) <= 1
    assert abs(y - (3403462 + 99124)) <= 1

def test_nested_groups_compose():
    outer = GroupFrame(off=(0, 0), ext=(1000, 1000), ch_off=(0, 0), ch_ext=(2000, 2000))
    inner = GroupFrame(off=(1000, 1000), ext=(1000, 1000), ch_off=(0, 0), ch_ext=(1000, 1000))
    # точка (500,500) внутри inner → (1500,1500) в системе outer → (750,750) в слайде
    assert resolve_point(500, 500, [outer, inner]) == (750, 750)

def test_zero_child_extent_does_not_divide_by_zero():
    frame = GroupFrame(off=(10, 10), ext=(100, 100), ch_off=(0, 0), ch_ext=(0, 0))
    assert resolve_point(50, 50, [frame]) == (10, 10)

def test_box_is_fraction_of_canvas():
    from deckforge.ooxml.geometry import Box, box_from_emu
    box = box_from_emu(left=6096000, top=0, width=6096000, height=6858000, canvas=CANVAS)
    assert box == Box(left=0.5, top=0.0, width=0.5, height=1.0)
    assert box.right == 1.0
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `uv run pytest tests/ooxml/test_geometry.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Реализовать `geometry.py`**

```python
# src/deckforge/ooxml/geometry.py
"""Координаты OOXML в долях холста.

python-pptx отдаёт координаты детей группы в системе координат группы и не
применяет преобразование. На шаблонах из разведки это 6, 28 и 19 групп,
всё их содержимое встало бы не на место, и без единого исключения.
"""
from __future__ import annotations
from dataclasses import dataclass

EMU_PER_INCH = 914400


@dataclass(frozen=True)
class Canvas:
    width_emu: int
    height_emu: int

    @property
    def width_in(self) -> float:
        return self.width_emu / EMU_PER_INCH

    @property
    def height_in(self) -> float:
        return self.height_emu / EMU_PER_INCH

    @property
    def ratio(self) -> float:
        return self.width_emu / self.height_emu

    @property
    def norm(self) -> float:
        """Множитель приведения кегля к холсту 13.333″.

        У VK Tech холст 10″, и 16pt на нём читаются как 21.3pt на обычном.
        Без нормировки типографические шкалы разных шаблонов несравнимы.
        """
        return 12192000 / self.width_emu


@dataclass(frozen=True)
class GroupFrame:
    off: tuple[int, int]
    ext: tuple[int, int]
    ch_off: tuple[int, int]
    ch_ext: tuple[int, int]


def resolve_point(x: int, y: int, chain: list[GroupFrame]) -> tuple[int, int]:
    """Применяет цепочку групп от внешней к внутренней."""
    for frame in reversed(chain):
        sx = frame.ext[0] / frame.ch_ext[0] if frame.ch_ext[0] else 0.0
        sy = frame.ext[1] / frame.ch_ext[1] if frame.ch_ext[1] else 0.0
        x = round(frame.off[0] + (x - frame.ch_off[0]) * sx)
        y = round(frame.off[1] + (y - frame.ch_off[1]) * sy)
    return x, y


@dataclass(frozen=True)
class Box:
    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def area(self) -> float:
        return self.width * self.height

    def intersect(self, other: "Box") -> "Box | None":
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Box(left, top, right - left, bottom - top)


def box_from_emu(left: int, top: int, width: int, height: int, canvas: Canvas) -> Box:
    return Box(left / canvas.width_emu, top / canvas.height_emu,
               width / canvas.width_emu, height / canvas.height_emu)
```

- [ ] **Step 4: Прогнать тесты геометрии**

Run: `uv run pytest tests/ooxml/test_geometry.py -v`
Expected: 5 passed

- [ ] **Step 5: Тест резолва цвета**

```python
# tests/ooxml/test_color.py
from lxml import etree
from deckforge.ooxml.color import resolve_color, Color

SCHEME = {"dk1": "#000000", "lt1": "#FFFFFF", "dk2": "#0077FF", "lt2": "#FFFFFF",
          "accent1": "#0077FF", "accent2": "#00E9FF"}
CLR_MAP = {"bg1": "lt1", "tx1": "dk1", "bg2": "dk2", "tx2": "lt2"}
A = "http://schemas.openxmlformats.org/drawingml/2006/main"

def node(xml: str):
    return etree.fromstring(f'<a:solidFill xmlns:a="{A}">{xml}</a:solidFill>')

def test_srgb():
    assert resolve_color(node('<a:srgbClr val="0077FF"/>'), SCHEME, CLR_MAP) == Color("#0077FF", 1.0)

def test_scheme_slot():
    assert resolve_color(node('<a:schemeClr val="accent1"/>'), SCHEME, CLR_MAP) == Color("#0077FF", 1.0)

def test_scheme_slot_through_clr_map():
    """Google пишет dk1 напрямую, PowerPoint пишет tx1 — резолвить надо оба."""
    assert resolve_color(node('<a:schemeClr val="tx1"/>'), SCHEME, CLR_MAP) == Color("#000000", 1.0)

def test_alpha_is_kept():
    got = resolve_color(node('<a:srgbClr val="0077FF"><a:alpha val="20000"/></a:srgbClr>'), SCHEME, CLR_MAP)
    assert got == Color("#0077FF", 0.2)

def test_lum_mod_darkens():
    got = resolve_color(node('<a:schemeClr val="accent1"><a:lumMod val="50000"/></a:schemeClr>'), SCHEME, CLR_MAP)
    assert got.hex == "#003B80"

def test_tint_lightens_towards_white():
    got = resolve_color(node('<a:srgbClr val="000000"><a:tint val="50000"/></a:srgbClr>'), SCHEME, CLR_MAP)
    assert got.hex == "#808080"

def test_no_fill_returns_none():
    n = etree.fromstring(f'<a:noFill xmlns:a="{A}"/>')
    assert resolve_color(n, SCHEME, CLR_MAP) is None
```

- [ ] **Step 6: Прогнать, убедиться что падает; реализовать `color.py`**

`resolve_color` разбирает `srgbClr`/`schemeClr`/`sysClr`/`prstClr`, применяет в порядке появления `alpha`, `lumMod`, `lumOff`, `shade`, `tint`, `satMod`; `noFill`, `grpFill` и `gradFill` возвращают `None` (градиент обрабатывается отдельно в `usage.py`). Слот `schemeClr` резолвится сначала через `clr_map`, затем напрямую по `scheme` — Google пишет `dk1`, PowerPoint `tx1`, и работать должны оба написания.

Run: `uv run pytest tests/ooxml/test_color.py -v`
Expected: 7 passed

- [ ] **Step 7: Тест пакета на реальных шаблонах**

```python
# tests/ooxml/test_package.py
import pytest
from pathlib import Path
from deckforge.ooxml.package import PptxPackage

TEMPLATES = sorted(Path("dataset/templates").glob("*.pptx"))

@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem[:20])
def test_opens_and_lists_masters(path):
    with PptxPackage.open(path) as pkg:
        masters = [n for n in pkg.names() if n.startswith("ppt/slideMasters/slideMaster")]
        assert masters, f"{path.name}: не найдено ни одного мастера"

def test_theme_is_resolved_through_rels_not_by_filename():
    """У VK WorkSpace theme1.xml — офисная заглушка, брендовая тема в theme2."""
    path = Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    with PptxPackage.open(path) as pkg:
        themes = pkg.related("ppt/slideMasters/slideMaster1.xml", "theme")
        assert themes == ["ppt/theme/theme2.xml"]

@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem[:20])
def test_media_inventory_is_not_empty(path):
    with PptxPackage.open(path) as pkg:
        media = pkg.media()
        assert media
        assert all(m.size_bytes > 0 for m in media)
```

- [ ] **Step 8: Реализовать `package.py` и `ns.py`, прогнать**

`PptxPackage` — обёртка над `zipfile.ZipFile` с кэшем разобранного XML и разбором `_rels/*.rels` (rId → `Target`, с нормализацией относительных путей). `MediaEntry(name, size_bytes, width, height, has_alpha, md5)` — размеры через Pillow, без раскрытия всего файла в память.

Run: `uv run pytest tests/ooxml/ -v`
Expected: все passed

- [ ] **Step 9: Коммит**

```bash
git add src/deckforge/ooxml tests/ooxml
git commit -m "feat(ooxml): пакет, резолв цвета DrawingML, аффинная геометрия групп"
```

---

### Task 3: Тема, детект деградации, фактическая палитра и шрифты

**Files:**
- Create: `src/deckforge/template/theme.py`, `src/deckforge/template/usage.py`
- Test: `tests/template/test_theme.py`, `tests/template/test_usage.py`

**Interfaces:**
- Consumes: `PptxPackage`, `resolve_color`, `Canvas`.
- Produces:
  - `read_theme(pkg, master_part: str) -> ThemeInfo` с полями `scheme: dict[str,str]`, `clr_map: dict[str,str]`, `major_font: str`, `minor_font: str`, `scheme_name: str`, `font_scheme_degraded: bool` (признак заглушки: имя схемы `Office`, major совпадает с minor, и шрифт темы почти не встречается в фактическом тексте; по именам шрифтов судить нельзя — Arial и Calibri в корпоративном шаблоне бывают осознанным выбором), `text_styles_degraded: bool`, `is_stock_office_palette: bool`.
  - `pick_primary_master(pkg) -> str` — часть с брендовой темой, а не первая попавшаяся.
  - `collect_usage(pkg, canvas) -> Usage` — тему для каждой части поднимает сама по графу связей (слайд → макет → мастер → тема). Единая тема на весь пакет неверна: в двух учебных шаблонах по два мастера с разными темами, и цвета макетов второго мастера резолвились бы чужой палитрой молча. `Usage` с полями `fill: Counter[Color]`, `text: Counter[Color]`, `line: Counter[Color]`, `layout_bg: Counter[Color]`, `fonts: dict[str, FontUsage]`, `sizes_pt: Counter[float]` (нормированные), `line_spacing: Counter[float]`, `align: Counter[str]`, `bold_runs: int`, `italic_runs: int`, `total_runs: int`.

- [ ] **Step 1: Тест на детект деградации**

```python
# tests/template/test_theme.py
import pytest
from pathlib import Path
from deckforge.ooxml.package import PptxPackage
from deckforge.template.theme import read_theme, pick_primary_master

TEMPLATES = sorted(Path("dataset/templates").glob("*.pptx"))

@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem[:20])
def test_google_export_font_scheme_is_flagged_degraded(path):
    """Во всех четырёх шаблонах fontScheme = Office/Arial. Парсер обязан это заметить,
    иначе построит типографику из заглушки."""
    with PptxPackage.open(path) as pkg:
        theme = read_theme(pkg, pick_primary_master(pkg))
        assert theme.font_scheme_degraded is True
        assert theme.text_styles_degraded is True

def test_workspace_primary_master_points_to_theme2():
    path = Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    with PptxPackage.open(path) as pkg:
        theme = read_theme(pkg, pick_primary_master(pkg))
        assert theme.scheme_name == "VK Tech 2024"
        assert theme.scheme["accent1"] == "#0077FF"
        assert theme.is_stock_office_palette is False

def test_stock_office_palette_is_detected():
    from deckforge.template.theme import is_stock_office
    assert is_stock_office({"accent1": "#4472C4", "accent2": "#ED7D31", "accent3": "#A5A5A5",
                            "accent4": "#FFC000", "accent5": "#5B9BD5", "accent6": "#70AD47"})
    assert not is_stock_office({"accent1": "#0077FF", "accent2": "#00E9FF", "accent3": "#AAFBFF",
                                "accent4": "#EDF3FC", "accent5": "#7C8A9A", "accent6": "#202020"})

@pytest.mark.parametrize("path,expected", [
    ("VK Tech шаблон.pptx", "#0077FF"),
    ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", "#0077FF"),
    ("Шаблон презентации VK Education.pptx", "#0077FF"),
])
def test_brand_accent_survives_extraction(path, expected):
    with PptxPackage.open(Path("dataset/templates") / path) as pkg:
        theme = read_theme(pkg, pick_primary_master(pkg))
        assert theme.scheme["accent1"] == expected
```

- [ ] **Step 2: Прогнать (FAIL), реализовать `theme.py`**

`text_styles_degraded` = True, когда все девять уровней `titleStyle` и `bodyStyle` дают одинаковую тройку (sz, latin, clr). `font_scheme_degraded` = True, когда `majorFont.latin == minorFont.latin` и оба ∈ {`Arial`, `Calibri`, `+mn-lt`} либо `fontScheme/@name == "Office"`. `pick_primary_master` выбирает мастер с наибольшим числом лейаутов, отбрасывая мастера, чья тема прошла `is_stock_office`; при равенстве — тот, на который ссылается больше слайдов.

Run: `uv run pytest tests/template/test_theme.py -v`
Expected: все passed

- [ ] **Step 3: Тест фактической палитры**

```python
# tests/template/test_usage.py
import pytest
from pathlib import Path
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.geometry import Canvas
from deckforge.template.theme import read_theme, pick_primary_master
from deckforge.template.usage import collect_usage

def load(name):
    pkg = PptxPackage.open(Path("dataset/templates") / name)
    theme = read_theme(pkg, pick_primary_master(pkg))
    canvas = pkg.canvas()
    return pkg, collect_usage(pkg, canvas, theme)

def test_workspace_usage_catches_colors_absent_from_theme():
    """У WorkSpace srgbClr на слайдах вдвое больше, чем schemeClr:
    палитра только из темы потеряет половину реальных цветов."""
    _, usage = load("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    hexes = {c.hex for c in usage.fill} | {c.hex for c in usage.text}
    assert "#212121" in hexes
    assert "#6DBCFF" in hexes

def test_alpha_fills_are_preserved():
    _, usage = load("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    assert any(c.hex == "#0077FF" and c.alpha < 0.5 for c in usage.fill)

def test_play_is_the_dominant_font_everywhere():
    for name in ["VK Tech шаблон.pptx", "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
                 "Шаблон презентации VK Education.pptx"]:
        _, usage = load(name)
        top = max(usage.fonts.values(), key=lambda f: f.chars)
        assert top.family == "Play", name

def test_embedded_fonts_are_reported():
    _, usage = load("VK Tech шаблон.pptx")
    assert usage.fonts["Play"].embedded is True
    assert usage.fonts["Play"].bold_available is True
    assert usage.fonts["Play"].italic_available is False

def test_bold_is_not_idiomatic_in_these_templates():
    """bold — 4 run'а из 744 у VK Tech. Генератор, ставящий bold «как обычно»,
    будет вылезать из шаблона."""
    _, usage = load("VK Tech шаблон.pptx")
    assert usage.bold_runs / usage.total_runs < 0.05
    assert usage.italic_runs == 0

def test_sizes_are_normalised_to_reference_canvas():
    """VK Tech свёрстан на холсте 10″; 24pt там — это 32pt в нормированной шкале."""
    _, usage = load("VK Tech шаблон.pptx")
    assert max(usage.sizes_pt) > 40
```

- [ ] **Step 4a: Устойчивость к нативному файлу PowerPoint**

Три учебных шаблона — экспорт из Google Slides, и разбор легко заточить под их особенности. На защите будет нативный файл, где по-другому устроено ровно то, что разбирает этот модуль:

- Шрифт указывается токенами `+mj-lt` и `+mn-lt`, а не именем. Токен резолвится в шрифт темы **всегда**, независимо от признака деградации: иначе шрифт теряется на больших кусках текста.
- Встречается `mc:AlternateContent`. Его ветки взаимоисключающие: наивный обход считает шейп дважды. Брать надо первый `mc:Choice`, чьё требуемое пространство имён мы понимаем, иначе `mc:Fallback`.
- Дефолтная тема Office бывает привязана к мастеру напрямую, а не сиротой. Отсев стоковой палитры обязан срабатывать, и это проверяется синтетическим пакетом, где стоковый мастер несёт больше макетов.
- Фон макета чаще наследуется от мастера. Отсутствие `p:bg` у макета — не повод его пропустить.
- Доля текста без явного `rPr` выше. Символы такого текста считаются всегда, неизвестные свойства уходят в отдельный счётчик «унаследовано», и его доля попадает в отчёт о разборе.

- [ ] **Step 4: Реализовать `usage.py`, прогнать**

Обход всех `slideLayout*.xml`, `slideMaster*.xml` и `slide*.xml`. Цвета текста взвешиваются **по числу символов**, а не run'ов — иначе заголовок из девяти знаков весит столько же, сколько абзац из сорока. Заливки и обводки считаются по числу шейпов. `noFill` учитывается отдельным счётчиком: полторы тысячи невидимых контейнеров у VK Tech — сигнал о карточной вёрстке, а не мусор. Кегли нормируются на `canvas.norm` и округляются до 0.5pt.

Run: `uv run pytest tests/template/test_usage.py -v`
Expected: все passed

- [ ] **Step 5: Коммит**

```bash
git add src/deckforge/template tests/template
git commit -m "feat(template): тема через rels, детект заглушек, фактическая палитра и шрифты"
```

---

### Task 4: Типографическая шкала и сетка

**Files:**
- Create: `src/deckforge/template/typography.py`, `src/deckforge/template/grid.py`
- Test: `tests/template/test_typography.py`, `tests/template/test_grid.py`

**Interfaces:**
- Consumes: `Usage`, `ThemeInfo`, `Canvas`, каталог лейаутов из Task 5 не нужен — шкала читается из `lstStyle` плейсхолдеров напрямую.
- Produces:
  - `build_type_scale(pkg, canvas, usage) -> TypeScale` с полями `steps: dict[str, float]` (ключи `display`, `h1`, `h2`, `body`, `caption`, `micro`), `heading_line_spacing: float`, `body_line_spacing: float` (оба с мерой уверенности: у интерлиньяжа есть тихий откат на 0.9 и 1.0, и отличить измеренное от подставленного по умолчанию обязательно), `default_align: str`, `bold_is_idiomatic: bool`, `italic_is_idiomatic: bool`, `families: list[str]` (не больше двух; считаются по нормализованному имени, с отброшенным хвостом начертания — иначе Montserrat и Montserrat Medium сойдут за две гарнитуры и проверка аудита T01 будет ложно срабатывать; полные имена сохраняются отдельно, они нужны при вёрстке).
  - `build_grid(pkg, canvas) -> Grid` с полями `margin_left/right/top/bottom: float`, `columns: list[ColumnAxis]` (не плоский список чисел: ось несёт центр в долях, число попаданий и меру уверенности, список отсортирован по весу и отсечён по порогу поддержки — плоский список на контрольном шаблоне раздувался до 136 осей, и потребитель не мог отличить настоящую колонку от совпадения координат внутри диаграммы), `gutter: float`, `anchors: dict[str, float]`, `confidence: dict[str, float]`.
  - `cluster(values: list[float], tolerance: float) -> list[Cluster]` — общая кластеризация одномерных координат.

- [ ] **Step 1: Тест шкалы**

```python
# tests/template/test_typography.py
def test_scale_is_monotonic_and_covers_six_steps(profile_fixture):
    scale = profile_fixture("VK Tech шаблон.pptx").type_scale
    steps = [scale.steps[k] for k in ("micro", "caption", "body", "h2", "h1", "display")]
    assert steps == sorted(steps)
    assert scale.steps["body"] >= 12

def test_headline_line_spacing_is_tighter_than_body():
    """Устойчивое правило во всех четырёх: заголовок 90%, body 100–110%."""
    for name in ALL_TEMPLATES:
        scale = profile_fixture(name).type_scale
        assert scale.heading_line_spacing <= scale.body_line_spacing

def test_no_more_than_two_families():
    """Проверка аудита T01 «гарнитур больше двух» опирается на этот список."""
    for name in ALL_TEMPLATES:
        assert len(profile_fixture(name).type_scale.families) <= 2

def test_workspace_body_scale_falls_back_to_runs():
    """У WorkSpace в лейаутах только title 36/54pt, body-шкалы нет вовсе —
    её нужно достроить из гистограммы run'ов, а не оставить пустой."""
    scale = profile_fixture("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx").type_scale
    assert 12 <= scale.steps["body"] <= 20
```

- [ ] **Step 2: Реализовать `typography.py`**

Источники по убыванию доверия: `a:lstStyle/a:lvl1pPr/a:defRPr@sz` плейсхолдеров лейаутов (нормированный) → гистограмма `sz` по run'ам слайдов → медиана по `ph_type`. `display` = максимум; `h1` = мода среди title-плейсхолдеров; `body` = мода среди run'ов body-плейсхолдеров, при их отсутствии — мода по символам среди всех run'ов; `caption` и `micro` — ближайшие ступени вниз. Шкала прореживается: два значения ближе 1.5pt схлопываются в одно. `families` — два самых весомых по символам шрифта, с отбрасыванием `Consolas`-подобных моноширинных в отдельное поле `mono`.

Run: `uv run pytest tests/template/test_typography.py -v`

- [ ] **Step 3: Тест сетки**

```python
# tests/template/test_grid.py
def test_margins_match_measured_values():
    """Числа замерены разведкой; допуск 0.5 п.п."""
    cases = [("VK Tech шаблон.pptx", 0.0463), ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", 0.0351),
             ("Шаблон презентации VK Education.pptx", 0.0540)]
    for name, expected in cases:
        grid = profile_fixture(name).grid
        assert abs(grid.margin_left - expected) < 0.005, name

def test_education_margins_are_symmetric():
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert abs(grid.margin_left - grid.margin_right) < 0.002

def test_education_has_exact_half_split():
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert any(abs(axis - 0.5) < 0.003 for axis in grid.columns)

def test_title_anchor_is_found():
    for name, expected in [("VK Tech шаблон.pptx", 0.055),
                           ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", 0.0619),
                           ("Шаблон презентации VK Education.pptx", 0.1009)]:
        assert abs(profile_fixture(name).grid.anchors["title_top"] - expected) < 0.008, name

def test_vertical_rhythm_is_reported_as_low_confidence():
    """Глобального baseline grid в этих файлах нет. Парсер обязан сказать об этом,
    а не выдать шум за сетку."""
    grid = profile_fixture("VK Tech шаблон.pptx").grid
    assert grid.confidence["baseline"] < 0.3
```

- [ ] **Step 4: Реализовать `grid.py`, прогнать**

Поле шаблона определяется не модальным кластером края, а отступом, начиная с которого на слайдах появляется содержание: низкий процентиль взвешенного распределения левых краёв. Слияние близких полос отменено — порог такого слияния неизбежно выводится из зазора в конкретном известном файле, и тест на него проходит тавтологически. Каждый оставшийся порог обосновывается долей содержимого или природой формата, а не наблюдением за тремя учебными файлами. Алгоритм обязан иметь синтетический тест с искусственным распределением и заранее известным ответом, независимый от шаблонов в датасете. Колонные оси — кластеры left-координат за вычетом полей, отобранные по числу попаданий ≥ 4; жёлоб — медиана расстояния между правым краем левой колонки и левым краем правой. Вертикальные якоря — модальные кластеры top по `ph_type` (title / body / footer). `confidence` считается как доля попаданий в кластер от всех замеров; для baseline-ритма считается отдельно и на этих файлах честно выходит низкой.

Run: `uv run pytest tests/template/test_grid.py -v`

- [ ] **Step 5: Коммит**

```bash
git commit -am "feat(template): типографическая шкала и восстановление сетки"
```

---

### Task 5: Каталог лейаутов и классификация типа

**Files:**
- Create: `src/deckforge/template/layouts.py`
- Test: `tests/template/test_layouts.py`

**Interfaces:**
- Consumes: `PptxPackage`, `ThemeInfo`, `Canvas`, `Grid`.
- Produces: `build_layout_catalog(pkg, canvas, theme, grid) -> list[LayoutEntry]`, где
  `LayoutEntry(layout_id: str, part_name: str, name: str, master_index: int, kind: str, kind_confidence: float, background: Background, is_dark: bool, placeholders: list[PlaceholderSlot], decor_count: int, asset_refs: list[str], usage_count: int)`.
  `kind` ∈ `{"title","section","content","two_col","three_col","quote","kpi","image","table","closing","free"}`.

- [ ] **Step 1: Тест классификации**

```python
# tests/template/test_layouts.py
def test_every_layout_gets_a_kind():
    for name in ALL_TEMPLATES:
        catalog = profile_fixture(name).layouts
        assert catalog
        assert all(entry.kind for entry in catalog)

def test_dark_layouts_are_detected_by_luminance_not_by_slot_name():
    """У VK Tech 23 из 39 лейаутов имеют фон schemeClr dk1 — это чёрный фон,
    а не чёрный текст."""
    catalog = profile_fixture("VK Tech шаблон.pptx").layouts
    dark = [e for e in catalog if e.is_dark]
    assert len(dark) >= 20

def test_workspace_meaningless_names_do_not_break_classification():
    """11 из 15 лейаутов WorkSpace называются «Титульный слайд», хотя это контентные
    раскладки. Классификация по имени даст 11 титульников — это ошибка."""
    catalog = profile_fixture("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx").layouts
    titles = [e for e in catalog if e.kind == "title"]
    assert len(titles) <= 3

def test_education_picture_placeholders_are_kept():
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").layouts
    with_picture = [e for e in catalog if any(p.ph_type == "PICTURE" for p in e.placeholders)]
    assert len(with_picture) >= 5

def test_placeholder_boxes_are_fractions():
    for name in ALL_TEMPLATES:
        for entry in profile_fixture(name).layouts:
            for slot in entry.placeholders:
                assert 0.0 <= slot.box.left <= 1.0
                assert 0.0 < slot.box.width <= 1.0
```

- [ ] **Step 2: Реализовать `layouts.py`**

Классификация ансамблем двух сигналов, потому что ни один по отдельности не работает: **имя** после срезания префикса `^\d+_` и приведения к нижнему регистру, по словарю синонимов (`титул|обложк|cover|title` → title; `раздел|секц|section|divider` → section; `цитат|quote` → quote; `спасибо|контакт|thanks|closing` → closing; и т.д.) и **геометрическая сигнатура**: число плейсхолдеров, доля площади самого крупного, кегль title относительно `type_scale.display`, симметрия относительно оси 0.5, наличие PICTURE, доля тёмного фона. Вклад имени — 0.6, геометрии — 0.4; при расхождении `kind_confidence` падает, и это поле дальше читает подборщик паттернов. Словарь синонимов лежит в `config/layout-kinds.yaml`, не в коде — на неизвестном шаблоне его правят без пересборки.

`is_dark` = яркость фона лейаута ниже 0.5 по формуле относительной яркости WCAG; фон резолвится по цепочке `p:bg` лейаута → `p:bg` мастера → `lt1`.

Run: `uv run pytest tests/template/test_layouts.py -v`

- [ ] **Step 3: Коммит**

```bash
git commit -am "feat(template): каталог лейаутов и классификация по имени и геометрии"
```

---

### Task 6: Каталог ассетов

**Files:**
- Create: `src/deckforge/template/assets.py`
- Test: `tests/template/test_assets.py`

**Interfaces:**
- Produces: `build_asset_catalog(pkg, canvas, layouts) -> AssetCatalog` с полями `logo: AssetRef | None`, `logo_placements: list[Placement]`, `backgrounds: list[AssetRef]`, `icons: list[AssetRef]`, `photos: list[AssetRef]`, `unclassified: list[AssetRef]`. `AssetRef(part_name, width, height, size_bytes, has_alpha, square, placements)`.

- [ ] **Step 1: Тест**

```python
# tests/template/test_assets.py
def test_logo_is_found_in_every_template():
    """Логотип = маленький файл, много размещений в лейаутах, угловая позиция."""
    for name in ALL_TEMPLATES:
        catalog = profile_fixture(name).assets
        assert catalog.logo is not None, name
        assert catalog.logo.size_bytes < 200_000

def test_education_icon_set_is_recognised():
    """206 изображений ровно 112×112 — иконочный сет опознаётся однозначно."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").assets
    assert len(catalog.icons) >= 100

def test_full_bleed_background_is_separated_from_icons():
    catalog = profile_fixture("VK Tech шаблон.pptx").assets
    assert catalog.backgrounds
    assert all(b.size_bytes > 100_000 for b in catalog.backgrounds)
    assert catalog.logo not in catalog.backgrounds

def test_logo_placement_is_recorded_for_the_audit():
    """Проверка T05 «логотип сдвинут с положенного места» сверяется с этим."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").assets
    assert catalog.logo_placements
    top = catalog.logo_placements[0]
    assert 0.0 <= top.box.left <= 0.3
```

- [ ] **Step 2: Реализовать `assets.py`**

Классификатор: **фон** — размещение ≥ 95% ширины и ≥ 95% высоты, либо файл > 300 КБ с размещением > 60% площади; **логотип** — файл < 200 КБ, размещён в лейаутах или мастере, ширина размещения 5–30% холста, центр в одной из четвертей у края, число размещений максимально; **иконка** — квадратная (отношение сторон 0.9–1.1), с альфой, размещение < 8% ширины; **фото** — всё остальное крупнее 50 КБ. Дедуп по md5 внутри файла (между шаблонами общих картинок нет, это проверено, так что глобальный дедуп бесполезен).

Run: `uv run pytest tests/template/test_assets.py -v`

- [ ] **Step 3: Коммит**

```bash
git commit -am "feat(template): классификация media на логотип, фон, иконки и фото"
```

---

### Task 7: Майнинг композиционных паттернов

Ядро решения. Плейсхолдеров в шаблонах почти нет, поэтому раскладки снимаются с готовых слайдов-примеров и превращаются в параметрические блоки.

**Files:**
- Create: `src/deckforge/template/patterns.py`
- Test: `tests/template/test_patterns.py`

**Interfaces:**
- Consumes: `PptxPackage`, `Canvas`, `Grid`, `TypeScale`, `AssetCatalog`, `walk_shapes`.
- Produces: `mine_patterns(pkg, canvas, grid, scale, assets) -> list[Pattern]`, где
  `Pattern(pattern_id, source_slide_index, layout_id, kind, slots: list[PatternSlot], repeat: RepeatSpec | None, decor: list[DecorShape], capacity: Capacity, score: float, is_dark: bool)`;
  `PatternSlot(role, box, size_pt, color_hex, align, max_chars, wraps)` где `role` ∈ `{"headline","subhead","body","bullet","card_title","card_body","kpi_value","kpi_label","quote","caption","source","image","icon","chart","table"}`;
  `RepeatSpec(axis: "x"|"y", count: int, step: float, slot_roles: list[str])`;
  `Capacity(max_items, max_chars_per_item, max_bullets, max_series, max_rows, max_cols)`.

- [ ] **Step 1: Тест майнинга**

```python
# tests/template/test_patterns.py
def test_patterns_are_mined_from_every_template():
    for name in ALL_TEMPLATES:
        patterns = profile_fixture(name).patterns
        assert len(patterns) >= 8, f"{name}: слишком мало паттернов, генератору не из чего выбирать"

def test_card_grid_is_detected_in_vk_tech():
    """1553 автошейпа, 1502 с noFill — это карточные композиции."""
    patterns = profile_fixture("VK Tech шаблон.pptx").patterns
    cards = [p for p in patterns if p.kind == "cards"]
    assert cards
    assert any(p.repeat and p.repeat.count >= 3 for p in cards)

def test_two_column_pattern_has_symmetric_slots():
    patterns = profile_fixture("Шаблон презентации VK Education.pptx").patterns
    two_col = [p for p in patterns if p.kind == "two_col"]
    assert two_col
    left, right = sorted(two_col[0].slots, key=lambda s: s.box.left)[:2]
    assert abs(left.box.width - right.box.width) < 0.02

def test_every_pattern_has_a_headline_slot_or_is_marked_decorative():
    for name in ALL_TEMPLATES:
        for pattern in profile_fixture(name).patterns:
            roles = {slot.role for slot in pattern.slots}
            assert "headline" in roles or pattern.kind in {"section", "image", "closing"}

def test_slots_respect_template_margins():
    for name in ALL_TEMPLATES:
        grid = profile_fixture(name).grid
        for pattern in profile_fixture(name).patterns:
            for slot in pattern.slots:
                assert slot.box.left >= grid.margin_left - 0.01
                assert slot.box.right <= 1 - grid.margin_right + 0.01

def test_capacity_is_derived_from_measured_box_not_guessed():
    """max_chars слота должен считаться замером текста в его рамке,
    иначе генератор напишет текст, который не влезет."""
    pattern = profile_fixture("VK Tech шаблон.pptx").patterns[0]
    headline = next(s for s in pattern.slots if s.role == "headline")
    assert 20 <= headline.max_chars <= 300

def test_soft_line_break_does_not_corrupt_slot_text():
    """В шаблонах перенос строки — a:br, который text_frame отдаёт как \\x0b."""
    for name in ALL_TEMPLATES:
        for pattern in profile_fixture(name).patterns:
            for slot in pattern.slots:
                assert "\x0b" not in (slot.sample_text or "")
```

- [ ] **Step 2: Реализовать `patterns.py`**

Алгоритм на каждый слайд-пример:

1. Обойти шейпы с аффинным резолвом групп, отбросить невидимое (нулевая площадь, за пределами холста).
2. Разделить на **контентные** (несут текст, картинку, таблицу) и **декор** (`noFill` без текста, линии, фоновые плашки, логотип). Декор сохраняется в `Pattern.decor` как есть и при сборке копируется вместе с паттерном — этим переносится визуальный язык шаблона.
3. Приписать каждому контентному шейпу роль по кеглю относительно `type_scale`, позиции относительно `grid.anchors`, цвету и порядку чтения (сверху вниз, слева направо).
4. Найти повторы: шейпы с одинаковой ролью, совпадающим размером (допуск 2%) и равным шагом по одной оси (допуск 0.5% холста) сворачиваются в `RepeatSpec`. Это и превращает шесть нарисованных карточек в один параметрический блок на N карточек.
5. Определить `kind` по сигнатуре: есть repeat по x с ≥ 3 повторами и парой title+body → `cards`; две колонки равной ширины → `two_col`; один крупный числовой слот → `kpi`; слот с кеглем ≥ `display` и без body → `section`; картинка > 40% площади → `image`; таблица → `table`; иначе `bullets`.
6. Посчитать `capacity` замером через `textfit` (общий модуль с аудитом, Task 9).
7. `score` — насколько паттерн пригоден для повторного использования: доля площади в полях, число ролей, отсутствие текста-рыбы, наличие repeat. Паттерны со `score` ниже порога отбрасываются.
8. Дедуплицировать: паттерны с совпадающим `kind` и попарно близкими боксами слотов схлопываются, `source_slide_index` накапливается.

Run: `uv run pytest tests/template/test_patterns.py -v`

- [ ] **Step 3: Коммит**

```bash
git commit -am "feat(template): майнинг композиционных паттернов со слайдов-примеров"
```

---

### Task 8: Сборка TemplateProfile, именование ролей моделью, CLI parse

**Files:**
- Create: `src/deckforge/template/profile.py`, `src/deckforge/template/naming.py`, `src/deckforge/cli.py`
- Create: `agents/palette-namer/AGENT.md`
- Test: `tests/template/test_profile.py`, `tests/test_cli_parse.py`

**Interfaces:**
- Produces: `TemplateProfile.from_file(path, *, namer: LLMProvider | None = None) -> TemplateProfile`; `.to_json() -> str`; `.fingerprint -> str`; `.provenance: list[str]`; `.warnings: list[str]`.
  `name_palette_roles(usage, theme, llm) -> dict[str, str]` возвращает отображение роль → hex для ролей `brand`, `surface`, `on_surface`, `accent`, `muted`, `danger`, `warning`, `border`.
- CLI: `deckforge parse <template.pptx> -o profile.json`.

- [ ] **Step 1: Тест профиля**

```python
# tests/template/test_profile.py
def test_profile_is_json_roundtrippable():
    profile = TemplateProfile.from_file(Path("dataset/templates/VK Tech шаблон.pptx"))
    assert TemplateProfile.model_validate_json(profile.to_json()) == profile

def test_provenance_explains_every_token():
    """Как отчёт разбора в aiva: человек должен видеть, откуда взято каждое значение."""
    profile = TemplateProfile.from_file(Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx"))
    joined = "\n".join(profile.provenance)
    assert "theme2" in joined
    assert "заглушк" in joined  # про деградировавший fontScheme

def test_warnings_flag_degraded_sources():
    profile = TemplateProfile.from_file(Path("dataset/templates/VK Tech шаблон.pptx"))
    assert any("txStyles" in w for w in profile.warnings)

def test_profile_never_falls_back_to_hardcoded_vk_values():
    """Страж требования «решение не заточено под три шаблона»."""
    import inspect, deckforge.template as pkg
    source = "".join(inspect.getsource(m) for m in _all_modules(pkg))
    for forbidden in ["#0077FF", "Play", "VK Tech", "VK Education", "WorkSpace"]:
        assert forbidden not in source, f"в парсере захардкожено {forbidden}"

def test_parsing_is_fast_enough():
    """Разбор входит в бюджет 5 минут на колоду с большим запасом."""
    import time
    start = time.monotonic()
    TemplateProfile.from_file(Path("dataset/templates/Шаблон презентации VK Education.pptx"))
    assert time.monotonic() - start < 30
```

- [ ] **Step 2: Написать `agents/palette-namer/AGENT.md`**

Файл-промпт, не строка в коде. Фронтматтер несёт версию — ТЗ требует версионирования агентов.

```markdown
---
name: palette-namer
version: 1.0.0
model_role: palette_namer
description: Присваивает частотным цветам шаблона семантические роли дизайн-системы.
---

Тебе дана палитра шаблона презентации: цвета из темы файла и цвета, реально
использованные на слайдах, с указанием контекста (заливка, текст, обводка, фон
макета) и частоты. Разложи их по ролям.

Роли: `brand` (главный фирменный), `accent` (второй фирменный, для выделения),
`surface` (фон слайда по умолчанию), `on_surface` (основной текст на этом фоне),
`muted` (второстепенный текст), `border` (тонкие линии и рамки),
`danger` (ошибка, отрицательная динамика), `warning` (предупреждение).

Правила:
- Роль назначается только цвету, который есть во входных данных. Новых не придумывай.
- `surface` и `on_surface` обязаны давать контраст не ниже 4.5:1. Контраст каждой
  пары посчитан за тебя и указан во входных данных — сверься с ним.
- Тёмный фон макетов означает, что `surface` тёмный, а `on_surface` светлый.
  Не переворачивай их по привычке.
- `danger` и `warning` оставь пустыми, если в палитре нет красного и жёлтого;
  выдумывать их из синего не надо.
- Ответ — один объект JSON вида `{"brand": "#RRGGBB", ...}`, без пояснений.
```

- [ ] **Step 3: Реализовать `profile.py`, `naming.py`, `cli.py`; прогнать**

`naming.py` читает `AGENT.md`, подставляет данные и валидирует ответ: hex должен присутствовать во входной палитре, контраст пары surface/on_surface пересчитывается кодом и при провале роль берётся детерминированным фолбэком (самый тёмный/светлый цвет с нужным контрастом). Модель предлагает, код проверяет.

Run: `set -a && . ./.env && set +a && uv run pytest tests/template/ -v`
Run: `uv run deckforge parse "dataset/templates/VK Tech шаблон.pptx" -o /tmp/vktech.json && python -c "import json;d=json.load(open('/tmp/vktech.json'));print(len(d['patterns']),'паттернов',len(d['layouts']),'лейаутов')"`
Expected: не меньше 8 паттернов, не меньше 30 лейаутов

- [ ] **Step 4: Записать золотые профили в `fixtures/expected/`**

```bash
for f in dataset/templates/*.pptx; do
  uv run deckforge parse "$f" -o "fixtures/expected/$(basename "${f%.pptx}").json"
done
git add fixtures/expected
git commit -m "feat(template): TemplateProfile, именование ролей моделью, CLI parse"
```

---
</content>

### Task 9: Замер текста и сборка слайда на лейауте шаблона

**Files:**
- Create: `src/deckforge/plan/spec.py`, `src/deckforge/compose/textfit.py`, `src/deckforge/compose/builder.py`, `src/deckforge/compose/blocks.py`, `src/deckforge/compose/decor.py`
- Test: `tests/compose/test_textfit.py`, `tests/compose/test_builder.py`

**Interfaces:**
- Consumes: `TemplateProfile`, `Pattern`, `PptxPackage`.
- Produces:
  - `DeckSpec(title, language, slides: list[SlideSpec], meta)`; `SlideSpec(index, kind, headline, subhead, blocks: list[Block], visual: Visual | None, source_note, speaker_notes)`; блоки `TextBlock(text)`, `BulletBlock(items)`, `CardBlock(items: list[Card])`, `KpiBlock(items: list[Kpi])`, `QuoteBlock(text, author)`.
  - `measure(text, font_family, size_pt, box_width_in) -> TextMetrics(lines, height_in, longest_word_in)` — единственный способ померить текст в проекте; им пользуются и сборка, и аудит, чтобы два расчёта одного и того же не расходились молча.
  - `build_deck(spec: DeckSpec, profile: TemplateProfile, template_path: Path, variant: Variant) -> Path` — сохраняет .pptx.
  - `place_slide(prs, slide_spec, pattern, profile) -> None`.
  - `fits(slide_spec, pattern, profile) -> Fit(ok: bool, overflow_ratio: float, reason: str)` — лезет ли содержание в раскладку. Когда ни одна не подходит, слайд уходит в песочницу (Task 10a).

- [ ] **Step 1: Тест замера**

```python
# tests/compose/test_textfit.py
from deckforge.compose.textfit import measure, font_file_for

def test_longer_text_wraps_into_more_lines():
    short = measure("Короткий", "Arial", 16, 4.0)
    long = measure("Очень длинный текст, который точно не поместится в одну строку", "Arial", 16, 4.0)
    assert long.lines > short.lines

def test_narrower_box_gives_more_lines():
    text = "Узкое место процесса — не работа, а ожидание между этапами"
    assert measure(text, "Arial", 16, 2.0).lines > measure(text, "Arial", 16, 6.0).lines

def test_height_grows_with_size():
    assert measure("Заголовок", "Arial", 32, 6.0).height_in > measure("Заголовок", "Arial", 16, 6.0).height_in

def test_soft_break_counts_as_a_line():
    assert measure("Первая\x0bвторая", "Arial", 16, 8.0).lines == 2

def test_unknown_font_falls_back_without_raising():
    metrics = measure("Текст", "Совершенно Несуществующий Шрифт", 16, 4.0)
    assert metrics.lines >= 1

def test_measurement_is_cached():
    """Без кэша слайд с таблицей 11×6 мерялся минутами."""
    import time
    text = "ячейка " * 20
    measure(text, "Arial", 12, 1.0)
    start = time.monotonic()
    for _ in range(500):
        measure(text, "Arial", 12, 1.0)
    assert time.monotonic() - start < 0.5
```

- [ ] **Step 2: Реализовать `textfit.py`**

Pillow `ImageFont.getlength` на метрически совместимом шрифте: сначала ищется сам шрифт шаблона (в том числе деобфусцированный `.fntdata`, см. Task 15), затем Liberation Sans, затем Arial, затем встроенный Pillow. `LINE_SPACING` берётся из `TypeScale`, а не константой. `@lru_cache` на загрузку шрифта и на замер строки.

Run: `uv run pytest tests/compose/test_textfit.py -v`

- [ ] **Step 3: Тест сборки**

```python
# tests/compose/test_builder.py
def test_slide_is_built_on_a_layout_from_the_template():
    """Проверка аудита T04: слайд обязан сидеть на макете шаблона, а не на пустом."""
    out = build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)
    prs = Presentation(str(out))
    template_layout_names = {e.name for e in PROFILE.layouts}
    for slide in prs.slides:
        assert slide.slide_layout.name in template_layout_names

def test_no_slide_is_a_single_raster_image():
    """ТЗ: слайд, выгруженный единым растровым изображением, не засчитывается."""
    prs = Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)))
    for slide in prs.slides:
        pictures = [s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE]
        others = [s for s in slide.shapes if s.shape_type != MSO_SHAPE_TYPE.PICTURE]
        assert others, "слайд состоит из одних картинок"

def test_text_never_leaves_its_box():
    prs = Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)))
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame or not shape.text_frame.text.strip():
                continue
            metrics = measure(shape.text_frame.text, _family(shape), _size(shape), shape.width / 914400)
            assert metrics.height_in <= shape.height / 914400 + 0.05

def test_only_template_colors_and_fonts_are_used():
    allowed_colors = PROFILE.palette.all_hexes()
    allowed_fonts = set(PROFILE.type_scale.families) | {PROFILE.type_scale.mono}
    for slide in Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense))).slides:
        for shape in slide.shapes:
            for run in _runs(shape):
                assert run.font.name in allowed_fonts
                if run.font.color and run.font.color.type is not None:
                    assert f"#{run.font.color.rgb}" in allowed_colors

def test_decor_of_the_pattern_is_carried_over():
    """Плашки и линии паттерна переносятся — иначе слайд теряет язык шаблона."""
    prs = Presentation(str(build_deck(CARDS_SPEC, PROFILE, TEMPLATE, Variant.visual)))
    cards_slide = prs.slides[2]
    assert len([s for s in cards_slide.shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]) >= 3

def test_template_master_shapes_are_not_duplicated():
    prs = Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)))
    for slide in prs.slides:
        logos = [s for s in slide.shapes if _is_logo(s, PROFILE)]
        assert len(logos) <= 1
```

- [ ] **Step 4: Реализовать `builder.py`, `blocks.py`, `decor.py`**

`build_deck` открывает **сам файл шаблона** как основу `Presentation(template_path)` и удаляет из него слайды-примеры, сохраняя мастера, лейауты, тему и media. Так лейауты, встроенные шрифты и палитра остаются нативными, и `T04` проходит по построению.

Для каждого `SlideSpec` подборщик берёт паттерн, уже выбранный на этапе планирования (Task 13), добавляет слайд на `pattern.layout_id`, копирует `pattern.decor` в XML как есть, затем кладёт контент в слоты. Текст, не влезающий в слот, ужимается по ступеням `type_scale` вниз, но не ниже `caption`; если и так не влезает — блок усекается, а в `SlideSpec` пишется finding, который увидит аудит. Никакого молчаливого обрезания.

`blocks.py` содержит укладчики по ролям слота: `headline`, `bullets` (буллет шаблона берётся из `pattern`, а не ставится `•` наугад), `cards` (разворачивает `RepeatSpec` под фактическое число элементов, пересчитывая шаг), `kpi`, `quote`, `caption`, `source`.

Run: `uv run pytest tests/compose/ -v`

- [ ] **Step 5: Коммит**

```bash
git commit -am "feat(compose): замер текста и сборка слайдов на лейаутах шаблона"
```

---

### Task 10: Нативные графики, таблицы, диаграммы и пиктограммы

ТЗ п.2.3: «Генерация графиков, таблиц, диаграмм, пиктограмм, элементов SmartArt внутри слайда». В .pptx — нативными объектами.

**Files:**
- Create: `src/deckforge/compose/charts.py`, `tables.py`, `diagrams.py`
- Test: `tests/compose/test_charts.py`, `test_tables.py`, `test_diagrams.py`

**Interfaces:**
- Produces:
  - `add_chart(slide, box, chart_spec: ChartSpec, profile) -> GraphicFrame`; `ChartSpec(kind, categories, series, unit, highlight_index, axis_titles)`, `kind` ∈ `{"bar","bar_stacked","bar_h","line","area","pie","doughnut","scatter"}`.
  - `add_table(slide, box, table_spec: TableSpec, profile) -> GraphicFrame`; `TableSpec(header, rows, align)`.
  - `add_diagram(slide, box, diagram_spec: DiagramSpec, profile) -> list[Shape]`; `kind` ∈ `{"process","cycle","hierarchy","funnel","comparison","timeline"}`.
  - `add_pictogram_row(slide, box, icons: list[str], profile) -> list[Shape]`.

- [ ] **Step 1: Тест графиков**

```python
# tests/compose/test_charts.py
def test_every_chart_kind_builds():
    for kind in ["bar", "bar_stacked", "bar_h", "line", "area", "pie", "doughnut", "scatter"]:
        frame = add_chart(new_slide(), BOX, ChartSpec(kind=kind, categories=["A","B","C"],
                          series=[Series("Выручка", [1,2,3])], unit="млн ₽"), PROFILE)
        assert frame.has_chart

def test_series_colors_come_from_the_template_palette():
    """Без явной окраски python-pptx отдаёт раскраску теме файла,
    и график выходит в цветах Office."""
    frame = add_chart(new_slide(), BOX, ChartSpec(kind="bar", categories=["A","B"],
                      series=[Series("s1",[1,2]), Series("s2",[2,1])]), PROFILE)
    used = _series_colors(frame.chart)
    assert set(used) <= set(PROFILE.palette.chart_series)

def test_single_series_colors_points_not_series():
    """У одного ряда окраска ряда даёт одинаковые столбцы; красить надо точки."""
    frame = add_chart(new_slide(), BOX, ChartSpec(kind="bar", categories=["A","B","C"],
                      series=[Series("s",[1,2,3])], highlight_index=1), PROFILE)
    colors = _point_colors(frame.chart.plots[0].series[0])
    assert len(set(colors)) == 2

def test_chart_has_axis_titles_and_units():
    """Проверка аудита I05: у диаграммы нет подписей осей, единиц или легенды."""
    frame = add_chart(new_slide(), BOX, ChartSpec(kind="line", categories=["Янв","Фев"],
                      series=[Series("Заявки",[10,12])], unit="штук",
                      axis_titles=("Месяц","Заявки")), PROFILE)
    assert _value_axis_title(frame.chart) == "Заявки, штук"

def test_legend_appears_only_when_it_carries_information():
    one = add_chart(new_slide(), BOX, ChartSpec(kind="bar", categories=["A"], series=[Series("s",[1])]), PROFILE)
    two = add_chart(new_slide(), BOX, ChartSpec(kind="bar", categories=["A"],
                    series=[Series("s1",[1]), Series("s2",[2])]), PROFILE)
    assert one.chart.has_legend is False
    assert two.chart.has_legend is True

def test_negative_values_move_category_labels_low():
    frame = add_chart(new_slide(), BOX, ChartSpec(kind="bar", categories=["A","B"],
                      series=[Series("s",[-5, 7])]), PROFILE)
    assert _tick_label_position(frame.chart.category_axis) == "LOW"
```

- [ ] **Step 2: Реализовать `charts.py`**

`CategoryChartData` + `add_chart`, затем явная окраска каждого ряда и каждой точки из `profile.palette.chart_series`; палитра рядов собирается в Task 8 из цветных токенов шаблона по убыванию веса, при нехватке достраивается оттенками `brand` (затемнение и осветление на 45% и 70%). Шрифт графика — `type_scale.families[0]`, кегль `caption`, цвет `on_surface`.

- [ ] **Step 3: Тест таблиц**

```python
# tests/compose/test_tables.py
def test_table_shrinks_font_until_it_fits_but_not_below_floor():
    frame = add_table(new_slide(), SMALL_BOX, TableSpec(header=["Метрика","Было","Стало"],
                      rows=[["Медианное время ожидания заявки в очереди","4 ч","1 ч"]]*6), PROFILE)
    sizes = _cell_sizes(frame.table)
    assert min(sizes) >= PROFILE.type_scale.steps["caption"] * 0.75

def test_row_heights_are_measured_not_split_evenly():
    """Высота строки в .pptx — минимум, а не размер: редактор растит строку под текст,
    но не ужимает, и таблица тихо уезжает на подвал."""
    frame = add_table(new_slide(), BOX, TableSpec(header=["A","B"],
                      rows=[["коротко","коротко"], ["очень длинный текст "*6, "x"]]), PROFILE)
    heights = [r.height for r in frame.table.rows]
    assert heights[2] > heights[1]

def test_header_text_color_is_chosen_by_contrast():
    frame = add_table(new_slide(), BOX, TableSpec(header=["A","B"], rows=[["1","2"]]), PROFILE)
    assert _contrast(_header_text_color(frame.table), PROFILE.palette.roles["accent"]) >= 4.5

def test_table_without_template_table_style_still_builds():
    """У VK Tech tableStyles.xml отсутствует вовсе."""
    profile = profile_fixture("VK Tech шаблон.pptx")
    frame = add_table(new_slide(), BOX, TableSpec(header=["A","B"], rows=[["1","2"]]), profile)
    assert frame.has_table
```

- [ ] **Step 4: Тест диаграмм и пиктограмм**

```python
# tests/compose/test_diagrams.py
def test_every_diagram_kind_builds_from_native_shapes():
    for kind in ["process", "cycle", "hierarchy", "funnel", "comparison", "timeline"]:
        shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind=kind,
                 items=["Заявка","Проверка","Согласование","Выдача"]), PROFILE)
        assert shapes
        assert all(s.shape_type != MSO_SHAPE_TYPE.PICTURE for s in shapes)

def test_diagram_uses_template_shape_geometry():
    """Скругление берётся из шаблона: если в нём только прямоугольники,
    рисовать скруглённые карточки — выход из дизайн-системы."""
    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="process", items=["A","B"]), PROFILE)
    assert all(s.auto_shape_type in PROFILE.shape_vocabulary for s in shapes)

def test_pictograms_come_from_the_template_icon_set():
    profile = profile_fixture("Шаблон презентации VK Education.pptx")
    shapes = add_pictogram_row(new_slide(), BOX, ["рост", "время", "команда"], profile)
    assert len(shapes) == 3

def test_pictograms_are_refused_when_template_has_no_icon_set():
    """Иконки либо из шаблона, либо их нет. Эмодзи и чужие наборы не подставляются."""
    profile = profile_fixture_without_icons()
    with pytest.raises(NoIconSet):
        add_pictogram_row(new_slide(), BOX, ["рост"], profile)
```

- [ ] **Step 5: Реализовать, прогнать, коммит**

`diagrams.py` строит SmartArt-подобные блоки из нативных автошейпов и соединителей: шесть раскладок, геометрия и скругление берутся из `profile.shape_vocabulary` (собирается в Task 7 как гистограмма `prstGeom` с их `avLst`). Настоящий `dgm:` SmartArt не создаётся сознательно — его формат не поддержан ни python-pptx, ни LibreOffice, и слайд разъехался бы при экспорте в PDF; блок из нативных шейпов редактируем в PowerPoint так же, как SmartArt, и это отражено в ARCHITECTURE.

Подбор иконки — по описанию, а не по имени файла: `assets.icons` получает текстовые описания на этапе Task 8 (VLM, одна картинка на иконку, кэш по md5), и подборщик ищет по ним.

Run: `uv run pytest tests/compose/ -v`

```bash
git commit -am "feat(compose): нативные графики, таблицы, диаграммы и пиктограммы"
```

---

### Task 10a: Песочница для слайдов, которые не лезут в раскладки

Снятых с шаблона раскладок хватает на типовые слайды, но не на всё. Когда содержание не влезает ни в одну, слайд собирает сам агент: пишет код в песочнице, где доступны только примитивы шаблона. Замер из aiva на той же задаче: код по спецификации даёт рабочий файл в 2 прогонах из 2 с нулём ошибок геометрии, код, написанный моделью без ограничений — 1 файл из 4 попыток и 28 ошибок. Отсюда песочница, а не свободное исполнение.

**Files:**
- Create: `src/deckforge/compose/sandbox.py`, `src/deckforge/compose/primitives.py`
- Create: `agents/slide-coder/AGENT.md`
- Modify: `src/deckforge/compose/builder.py` (ветка запасного пути)
- Test: `tests/compose/test_sandbox.py`

**Interfaces:**
- Consumes: `TemplateProfile`, `SlideSpec`, `Fit`, `run_deterministic` из Task 11.
- Produces: `code_slide(prs, slide_spec, profile, llm, *, attempts: int = 2, budget_s: float = 40) -> SlideOutcome`; `SlideOutcome(built: bool, findings: list[Finding], code: str, attempts_used: int, fell_back_to: str | None)`.
- `primitives.py` — единственное, что видно коду агента: `text(box, content, role)`, `plate(box, role)`, `image(box, asset_id)`, `icon(box, name)`, `chart(box, spec)`, `table(box, spec)`, `divider(box)`, `grid(columns, gutter)`, плюс константы профиля только для чтения. Ни `python-pptx`, ни файловой системы, ни сети.

- [ ] **Step 1: Тест песочницы**

Шесть случаев, каждый отдельным тестом в `tests/compose/test_sandbox.py`:

1. `test_generated_code_cannot_import_anything` — код с `import os` не исполняется, в findings попадает причина.
2. `test_generated_code_cannot_touch_python_pptx_directly` — обращение к `slide.shapes.add_textbox` отвергается: у кода нет доступа ни к `prs`, ни к `slide`.
3. `test_only_template_colors_reach_the_slide` — примитив не принимает произвольный цвет, только роль из профиля; множество цветов на готовом слайде вложено в палитру шаблона.
4. `test_failed_audit_triggers_a_rewrite_then_falls_back` — модель, которая всегда переполняет рамку: две попытки, затем откат на ближайшую раскладку. `fell_back_to` не пуст, `built` истинно. Пустой слайд не выдаётся никогда.
5. `test_sandbox_respects_the_time_budget` — общий бюджет колоды пять минут, песочница не имеет права его съесть: при `budget_s=40` вызов укладывается в 50 секунд даже с медленной моделью.
6. `test_infinite_loop_in_generated_code_is_stopped` — `while True: pass` останавливается по лимиту, в findings причина про время.

- [ ] **Step 2: Написать `agents/slide-coder/AGENT.md`**

Промпт получает: содержание слайда, профиль шаблона (палитра с ролями, шкала кеглей, сетка, каталог ассетов), список доступных примитивов с сигнатурами, и причину, по которой не подошла ни одна готовая раскладка. На второй попытке — ещё и findings аудита с первой.

Правила в промпте: координаты только в долях холста и только внутри полей из профиля; цвет задаётся ролью, не значением; кегль берётся из шкалы, промежуточных значений нет; жирное начертание ставится, только если в шаблоне оно идиоматично, и профиль это знает; никаких импортов и обращений к `prs` напрямую.

- [ ] **Step 3: Реализовать песочницу**

Исполнение через `exec` с пустым `__builtins__`, кроме белого списка, и namespace из `primitives.py`. Жёсткий лимит времени и числа операций. После исполнения слайд немедленно прогоняется через `run_deterministic`: находки уровня error означают неудачу попытки. Две попытки, затем откат на раскладку с наименьшим переполнением.

Код каждого слайда сохраняется в артефакты задания: на защите нужно показать, что именно агент написал.

- [ ] **Step 4: Коммит**

```bash
git commit -am "feat(compose): песочница для слайдов, не влезающих в снятые раскладки"
```

---

### Task 11: Детерминированный аудит

Приложение 1 ТЗ, раздел «Верификация параметров». Каждая проверка имеет идентификатор, и он же попадает в AUDIT.md.

**Files:**
- Create: `src/deckforge/audit/findings.py`, `deterministic.py`
- Create: `config/audit.yaml` — пороги отдельным файлом, ТЗ разрешает расширять и сокращать набор с обоснованием
- Test: `tests/audit/test_deterministic.py`

**Interfaces:**
- Produces: `Finding(check_id, severity, slide_index, shape_ref, message, box: Box | None, fixable: bool, fix_hint: str)`; `run_deterministic(pptx_path, profile, config) -> list[Finding]`.

Реестр проверок:

| id | группа | что ловит |
|---|---|---|
| L01 | вёрстка | элемент вышел за границы слайда |
| L02 | вёрстка | два блока наложились друг на друга |
| L03 | вёрстка | текст не поместился в свою рамку |
| L04 | вёрстка | текст обрезан краем слайда |
| L05 | вёрстка | блоки не выровнены по направляющим макета |
| L06 | вёрстка | контент заходит в поля у краёв |
| L07 | вёрстка | картинка растянута, пропорции нарушены |
| T01 | шаблон | шрифт не из шаблона или гарнитур больше двух |
| T02 | шаблон | кегль не из типографической шкалы шаблона |
| T03 | шаблон | цвет не из палитры шаблона |
| T04 | шаблон | слайд собран не на макете из шаблона |
| T05 | шаблон | логотип или колонтитул сдвинуты с положенного места |
| T06 | шаблон | контраст текста к фону ниже 4.5:1 |
| D01 | плотность | больше 6 буллетов на слайде |
| D02 | плотность | буллет длиннее 15 слов |
| D03 | плотность | таблица больше 7 строк или 5 колонок |
| D04 | плотность | больше 5 серий на диаграмме |
| D05 | плотность | слайд заполнен меньше чем на четверть или больше чем на три четверти |
| I01 | целостность | файл не открывается |
| I02 | целостность | остался текст-заглушка: lorem ipsum, XXX, TODO, «вставьте текст» |
| I03 | целостность | пустой слайд или слайд с одним заголовком |
| I04 | целостность | слайд оказался картинкой, а не редактируемыми объектами |
| I05 | целостность | у диаграммы нет подписей осей, единиц или легенды |
| I06 | целостность | два слайда дублируют друг друга |

- [ ] **Step 1: Тест по каждой проверке**

Тест строит колоду с заведомым дефектом и требует ровно этот finding. Ниже — образцы, в реализации нужен один тест на каждый из 24 идентификаторов.

```python
# tests/audit/test_deterministic.py
def test_L01_catches_shape_outside_the_slide():
    path = deck_with(lambda s: s.shapes.add_textbox(Inches(12), Inches(1), Inches(3), Inches(1)))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"L01"}

def test_L02_ignores_adjacent_blocks_but_catches_real_overlap():
    """Порог замерен: наезд 0.02″ даёт 6.7% площади меньшего блока и глазом не виден,
    наезд 0.06″ даёт 20% и уже заметен. Порог 7% проходит между ними."""
    assert "L02" not in _ids(run_deterministic(deck_with_adjacent_blocks(), PROFILE, CONFIG))
    assert "L02" in _ids(run_deterministic(deck_with_overlap(0.06), PROFILE, CONFIG))

def test_L02_does_not_flag_text_on_its_own_plate():
    assert "L02" not in _ids(run_deterministic(deck_with_text_on_plate(), PROFILE, CONFIG))

def test_L02_does_flag_text_on_top_of_a_chart():
    assert "L02" in _ids(run_deterministic(deck_with_text_over_chart(), PROFILE, CONFIG))

def test_L07_catches_stretched_picture():
    path = deck_with_picture(native=(800, 600), placed=(Inches(8), Inches(2)))
    assert "L07" in _ids(run_deterministic(path, PROFILE, CONFIG))

def test_T03_catches_color_outside_the_palette():
    assert "T03" in _ids(run_deterministic(deck_with_color("#FF00FF"), PROFILE, CONFIG))

def test_T06_measures_contrast_against_the_plate_under_the_text():
    """Фон берётся тот, что реально под текстом: своя заливка → объемлющая плашка
    → подложка слайда → фон макета."""
    assert "T06" not in _ids(run_deterministic(deck_light_text_on_dark_plate(), PROFILE, CONFIG))
    assert "T06" in _ids(run_deterministic(deck_light_text_on_light_plate(), PROFILE, CONFIG))

def test_D05_flags_both_empty_and_overstuffed_slides():
    assert "D05" in _ids(run_deterministic(deck_filled(0.15), PROFILE, CONFIG))
    assert "D05" in _ids(run_deterministic(deck_filled(0.85), PROFILE, CONFIG))
    assert "D05" not in _ids(run_deterministic(deck_filled(0.5), PROFILE, CONFIG))

def test_I04_flags_a_slide_that_is_one_big_picture():
    assert "I04" in _ids(run_deterministic(deck_single_raster(), PROFILE, CONFIG))

def test_I06_flags_duplicate_slides():
    assert "I06" in _ids(run_deterministic(deck_with_duplicate_slides(), PROFILE, CONFIG))

def test_clean_deck_produces_no_findings():
    assert run_deterministic(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense), PROFILE, CONFIG) == []

def test_same_file_gives_the_same_result_twice():
    """ТЗ: детерминированная проверка на одном и том же слайде всегда даёт один результат."""
    path = build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)
    assert run_deterministic(path, PROFILE, CONFIG) == run_deterministic(path, PROFILE, CONFIG)
```

- [ ] **Step 2: Написать `config/audit.yaml`**

```yaml
# Пороги аудита. Вынесены из кода: ТЗ разрешает расширять и сокращать
# набор проверок с обоснованием, и обоснование должно лежать рядом с числом.
layout:
  overlap_ratio: 0.07          # доля площади меньшей из двух фигур; замер: 0.02″ даёт 6.7%, 0.06″ даёт 20%
  overlap_min_area_in2: 0.01
  margin_tolerance: 0.005      # доля ширины холста
  grid_tolerance: 0.008        # допуск выравнивания по восстановленной сетке
  aspect_tolerance: 0.03       # растяжение картинки
template:
  min_contrast_small: 4.5
  min_contrast_large: 3.0
  large_text_pt: 18
  large_bold_pt: 14
  max_font_families: 2
  size_tolerance_pt: 0.5
density:
  max_bullets: 6
  max_words_per_bullet: 15
  max_table_rows: 7
  max_table_cols: 5
  max_chart_series: 5
  fill_ratio_min: 0.25
  fill_ratio_max: 0.75
integrity:
  placeholder_patterns: ["lorem ipsum", "XXX", "TODO", "вставьте текст", "текст заголовка", "ваш текст"]
  duplicate_similarity: 0.9
```

- [ ] **Step 3: Реализовать `deterministic.py`, прогнать**

Всё считается по XML файла, не по картинке. Площадь текстового блока для L02 и D05 берётся **измеренная** (`textfit`), а не объявленная: сборка нарочно даёт блокам запас по высоте, и объявленная рамка дала бы ложные наложения. Плашка под текстом определяется как автошейп без текста, целиком накрывающий блок; из нескольких берётся самая маленькая.

Run: `uv run pytest tests/audit/test_deterministic.py -v`
Expected: 24+ passed

```bash
git commit -am "feat(audit): 24 детерминированные проверки по Приложению 1"
```

---

### Task 12: Рендер через LibreOffice и модельный аудит по картинке

**Files:**
- Create: `src/deckforge/render/soffice.py`, `src/deckforge/audit/visual.py`, `src/deckforge/audit/report.py`
- Create: `agents/content-auditor/AGENT.md`
- Test: `tests/render/test_soffice.py`, `tests/audit/test_visual.py`

**Interfaces:**
- Produces: `to_pdf(pptx: Path, out_dir: Path) -> Path`; `to_pngs(pptx: Path, out_dir: Path, dpi: int = 110) -> list[Path]`; `run_visual(pngs, spec, profile, vlm) -> list[Finding]`; `AuditReport.merge(deterministic, visual) -> AuditReport`.

Одиннадцать недетерминированных проверок, дословно из ТЗ, идентификаторы C01–C11:

1. Заголовок содержит вывод, а не просто называет тему?
2. Содержимое слайда соответствует заголовку?
3. Слайд пересказывается одним предложением?
4. Все цифры и факты со слайда есть в исходных материалах?
5. На слайде есть содержание, а не только заголовок?
6. Картинки и иконки относятся к теме слайда?
7. Нет служебного мусора: реплик спикера, кусков промпта?
8. Текст без опечаток?
9. Вся колода на одном языке?
10. Все строки таблицы и элементы легенды работают на мысль слайда?
11. Соседние слайды связаны между собой по логике?

- [ ] **Step 1: Тест рендера**

```python
# tests/render/test_soffice.py
@pytest.mark.skipif(not soffice_available(), reason="LibreOffice не установлен")
def test_pptx_converts_to_pdf_with_the_same_page_count():
    pptx = build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)
    pdf = to_pdf(pptx, tmp_path)
    assert pdf.exists()
    assert _pdf_pages(pdf) == len(SAMPLE_SPEC.slides)

@pytest.mark.skipif(not soffice_available(), reason="LibreOffice не установлен")
def test_one_png_per_slide():
    pngs = to_pngs(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense), tmp_path)
    assert len(pngs) == len(SAMPLE_SPEC.slides)
    assert all(Image.open(p).width >= 1000 for p in pngs)

def test_parallel_conversions_do_not_fight_over_the_profile():
    """Два soffice без своего -env:UserInstallation дерутся за блокировку профиля."""
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda i: to_pdf(SAMPLE_PPTX, tmp_path / str(i)), range(4)))
    assert all(p.exists() for p in results)
```

- [ ] **Step 2: Реализовать `soffice.py`**

Путь к `soffice` — из `config/app.yaml`, с автопоиском `/Applications/LibreOffice.app/Contents/MacOS/soffice`, `soffice`, `libreoffice`. На каждый вызов свой `-env:UserInstallation=file:///tmp/...` и свой временный `FONTCONFIG_FILE`, в который добавлен каталог со шрифтами шаблона (деобфусцированными из `ppt/fonts/*.fntdata`) — иначе LibreOffice подставит свой шрифт и превью разойдётся с файлом. PNG получаются через PDF (`pdftoppm`, если есть) либо напрямую `--convert-to png`.

- [ ] **Step 3: Написать `agents/content-auditor/AGENT.md`**

```markdown
---
name: content-auditor
version: 1.0.0
model_role: content_audit
description: Отвечает «да» или «нет» на одиннадцать вопросов о слайде по его изображению.
---

Тебе дана картинка одного слайда презентации и служебные данные: исходные
материалы, по которым слайд собран, текст соседних слайдов и язык колоды.

Ответь на каждый вопрос строго «да» или «нет». Где ответ «нет» — одной фразой
назови конкретное место на слайде. Общих слов вроде «можно улучшить» не пиши.

1. Заголовок содержит вывод, а не просто называет тему?
2. Содержимое слайда соответствует заголовку?
3. Слайд пересказывается одним предложением?
4. Все цифры и факты со слайда есть в исходных материалах?
5. На слайде есть содержание, а не только заголовок?
6. Картинки и иконки относятся к теме слайда?
7. Нет служебного мусора: реплик спикера, кусков промпта?
8. Текст без опечаток?
9. Вся колода на одном языке?
10. Все строки таблицы и элементы легенды работают на мысль слайда?
11. Соседние слайды связаны между собой по логике?

Ответ — объект JSON: `{"C01": {"ok": true}, "C04": {"ok": false, "where": "цифра 42% в правой карточке, в исходных материалах её нет"}, ...}`.
Ключи C01…C11 соответствуют номерам вопросов. Никакого текста вне JSON.
```

- [ ] **Step 4: Тест визуального аудита**

```python
# tests/audit/test_visual.py
@live
def test_missing_body_is_caught():
    """C05: на слайде есть содержание, а не только заголовок."""
    findings = run_visual(to_pngs(deck_with_title_only(), tmp_path), SPEC, PROFILE, VLM)
    assert "C05" in {f.check_id for f in findings}

@live
def test_invented_number_is_caught():
    """C04: все цифры со слайда есть в исходных материалах."""
    findings = run_visual(to_pngs(deck_with_number_absent_from_sources(), tmp_path), SPEC, PROFILE, VLM)
    assert "C04" in {f.check_id for f in findings}

@live
def test_clean_slide_passes_most_checks():
    findings = run_visual(to_pngs(GOOD_DECK, tmp_path), SPEC, PROFILE, VLM)
    assert len(findings) <= 2

def test_visual_audit_degrades_loudly_without_a_vision_model():
    """Если выбранная модель не мультимодальна, аудит обязан сказать об этом,
    а не молча вернуть пустой список и создать видимость проверки."""
    report = run_visual(PNGS, SPEC, PROFILE, TextOnlyProvider())
    assert report.skipped_reason and "мультимодал" in report.skipped_reason

def test_malformed_model_answer_does_not_crash_the_pipeline():
    findings = run_visual(PNGS, SPEC, PROFILE, BrokenJsonProvider())
    assert all(f.check_id == "C00" for f in findings)
```

- [ ] **Step 5: Реализовать, прогнать, коммит**

Слайды проверяются пачками параллельно (по умолчанию 4 потока), C09 и C11 задаются один раз на колоду, а не на каждый слайд. Ответ модели валидируется схемой; невалидный ответ даёт один finding `C00` с текстом ответа, а не тишину.

```bash
git commit -am "feat(audit): рендер через LibreOffice и одиннадцать модельных проверок"
```

---

### Task 13: Планирование содержания и три варианта вёрстки

**Files:**
- Create: `src/deckforge/plan/outline.py`, `writer.py`, `variants.py`
- Create: `agents/outline-writer/AGENT.md`, `agents/slide-writer/AGENT.md`, `agents/pattern-picker/AGENT.md`
- Test: `tests/plan/test_outline.py`, `test_variants.py`

**Interfaces:**
- Produces: `build_outline(brief: str, sources: list[SourceDoc], profile, llm, target_slides: int | None) -> Outline`; `write_slides(outline, sources, profile, llm) -> DeckSpec`; `pick_patterns(deck_spec, profile, llm) -> DeckSpec` (проставляет `pattern_id` каждому слайду); `Variant` — перечисление `dense | airy | visual`; `apply_variant(deck_spec, profile, variant) -> DeckSpec`.

Три оси различия вариантов, как требует ТЗ («ось различий команда определяет сама… и обосновывает свой выбор в документации»):

| Вариант | Плотность | Подбор паттернов | Визуализация данных | Группировка |
|---|---|---|---|---|
| `dense` | максимум фактов на слайд | предпочитает `cards`, `two_col`, `table` | таблицы и составные графики | минимум разделителей, 10–11 слайдов |
| `airy` | один тезис на слайд | предпочитает `section`, `kpi`, `quote` | крупная цифра вместо графика | разделитель перед каждым блоком, 14–15 слайдов |
| `visual` | текст подчинён картинке | предпочитает `image`, `cards` с иконками, `process` | диаграммы и пиктограммы вместо списков | 12–13 слайдов |

Все три собираются из **одного** `DeckSpec` и **одного** `TemplateProfile`, различаясь только подбором паттернов и разбиением контента. Это и даёт «визуально различимы, но одинаково соответствуют правилам шаблона».

- [ ] **Step 1: Тест вариантов**

```python
# tests/plan/test_variants.py
def test_three_variants_are_visually_distinct():
    specs = [apply_variant(DECK, PROFILE, v) for v in Variant]
    pattern_sets = [{s.pattern_id for s in spec.slides} for spec in specs]
    for a, b in combinations(pattern_sets, 2):
        assert len(a ^ b) >= 3, "варианты используют почти одни и те же паттерны"

def test_three_variants_carry_the_same_facts():
    """Различаться должна вёрстка, а не содержание: иначе сравнивать нечего."""
    facts = [_numbers(spec) for spec in (apply_variant(DECK, PROFILE, v) for v in Variant)]
    assert facts[0] == facts[1] == facts[2]

def test_every_variant_stays_within_the_slide_budget():
    for variant in Variant:
        count = len(apply_variant(DECK, PROFILE, variant).slides)
        assert 10 <= count <= 15

def test_every_variant_passes_the_deterministic_audit():
    """Одинаково соответствуют правилам шаблона — это проверяется, а не декларируется."""
    for variant in Variant:
        path = build_deck(apply_variant(DECK, PROFILE, variant), PROFILE, TEMPLATE, variant)
        errors = [f for f in run_deterministic(path, PROFILE, CONFIG) if f.severity == "error"]
        assert errors == [], f"{variant}: {errors}"

def test_variant_falls_back_when_template_lacks_a_pattern_kind():
    """У шаблона может не быть ни одного image-паттерна. Вариант visual обязан
    деградировать на доступные, а не упасть."""
    poor = profile_fixture_without_image_patterns()
    spec = apply_variant(DECK, poor, Variant.visual)
    assert all(s.pattern_id for s in spec.slides)
```

- [ ] **Step 2: Написать `agents/outline-writer/AGENT.md`**

```markdown
---
name: outline-writer
version: 1.0.0
model_role: outline
description: Превращает бриф и исходные материалы в структуру колоды.
---

Ты составляешь структуру презентации: сколько слайдов, о чём каждый, в каком
порядке. Текст слайдов писать не надо — это следующий шаг.

Твоя работа — содержание и порядок. Вёрстку, цвета, шрифты и координаты делает
код по разобранному шаблону. Не предлагай оформление, не выбирай цвета,
не описывай, как слайд выглядит.

Правила структуры:
- Первый слайд — титульный, последний — итоговый.
- Каждый содержательный слайд несёт ровно одну мысль, и эта мысль —
  утверждение, а не тема. «Узкое место — не работа, а ожидание», а не
  «Анализ процесса».
- Соседние слайды связаны: следующий продолжает или уточняет предыдущий.
- Не ставь подряд два слайда одного типа, если это не осознанная серия.
- Цифры бери только из исходных материалов. Если цифры нет — не выдумывай,
  а отметь слайд как требующий данных.

На входе: бриф, назначение колоды (фича, продукт, проект, инициатива),
исходные материалы, целевое число слайдов.

Ответ — JSON: `{"slides": [{"kind": "...", "intent": "...", "needs": ["..."]}]}`,
где `kind` — один из: title, agenda, context, problem, solution, how_it_works,
data, comparison, case, roadmap, team, risks, ask, closing.
```

- [ ] **Step 3: Написать `agents/slide-writer/AGENT.md` и `agents/pattern-picker/AGENT.md`**

`slide-writer` получает пункт структуры и пишет текст: заголовок-вывод, подзаголовок с оговоркой или единицей измерения, блоки контента, строку источника при наличии цифр. Лимиты длины приходят из `capacity` выбранного паттерна, то есть из замера реальной рамки в шаблоне, а не из головы.

`pattern-picker` получает **список доступных паттернов шаблона** с их вместимостью и содержание слайда, и выбирает паттерн. Выбор ограничен списком: паттерна вне шаблона модель предложить не может физически, а если предложит — код отвергнет и возьмёт лучший по вместимости.

- [ ] **Step 4: Реализовать, прогнать, коммит**

Валидатор `DeckSpec` ловит то, что иначе всплывёт только при сборке: незнакомое поле (опечатка — ошибка, а не тишина), пустой обязательный текст, расхождение длины ряда данных с числом категорий, расхождение длины строки таблицы с шапкой, отсутствие `source` при наличии цифр.

Run: `set -a && . ./.env && set +a && uv run pytest tests/plan/ -v`

```bash
git commit -am "feat(plan): планирование содержания и три варианта вёрстки"
```

---

### Task 14: Экспорт в html, pptx, pdf

**Files:**
- Create: `src/deckforge/export/html.py`, `bundle.py`
- Test: `tests/export/test_html.py`

**Interfaces:**
- Produces: `to_html(deck_spec, profile, pptx_path, out: Path) -> Path`; `export_bundle(pptx_path, profile, out_dir) -> Bundle(pptx, pdf, html)`.

- [ ] **Step 1: Тест**

```python
# tests/export/test_html.py
def test_html_is_a_single_self_contained_file():
    html = to_html(DECK, PROFILE, PPTX, tmp_path / "deck.html")
    text = html.read_text(encoding="utf-8")
    assert "<script src=\"http" not in text
    assert "<link rel=\"stylesheet\" href=\"http" not in text

def test_html_slides_are_text_not_screenshots():
    """То же требование, что и к pptx: слайд не должен быть картинкой."""
    text = to_html(DECK, PROFILE, PPTX, tmp_path / "deck.html").read_text(encoding="utf-8")
    assert DECK.slides[1].headline in text

def test_html_uses_template_tokens():
    text = to_html(DECK, PROFILE, PPTX, tmp_path / "deck.html").read_text(encoding="utf-8")
    assert PROFILE.palette.roles["brand"].lower() in text.lower()
    assert PROFILE.type_scale.families[0] in text

def test_bundle_has_all_three_formats():
    bundle = export_bundle(PPTX, PROFILE, tmp_path)
    assert bundle.pptx.exists() and bundle.pdf.exists() and bundle.html.exists()
```

- [ ] **Step 2: Реализовать**

HTML собирается из `DeckSpec` и `TemplateProfile`, а не из скриншотов: слайд — это `<section>` с абсолютно спозиционированными блоками в тех же долях холста, шрифт шаблона вшивается base64 из `ppt/fonts/`, токены уезжают в CSS-переменные на `:root`. Навигация стрелками, режим обзора, печать в PDF из браузера. Одна страница, без внешних запросов — её можно открыть с флешки на защите.

Run: `uv run pytest tests/export/ -v`

```bash
git commit -am "feat(export): html, pdf и pptx одним пакетом"
```

---

### Task 15: Форк Mini-Agent, скиллы и версионирование

ТЗ п.2.4: «Версионирование скиллов и агентов, лежащих в основе воркфлоу». П.4: промпты и конфиги лежат отдельными файлами.

**Files:**
- Create: `vendor/mini_agent/` (форк MiniMax-AI/Mini-Agent, MIT, коммит зафиксирован в `vendor/MINI_AGENT_COMMIT`)
- Create: `skills/template-decompose/SKILL.md`, `skills/deck-compose/SKILL.md`, `skills/deck-audit/SKILL.md`
- Create: `src/deckforge/agent/tools.py` — инструменты поверх `mini_agent.tools.base.Tool`
- Create: `src/deckforge/agent/versions.py` — реестр версий скиллов и агентов
- Test: `tests/agent/test_versions.py`, `tests/agent/test_tools.py`

**Interfaces:**
- Produces: `ParseTemplateTool`, `PlanDeckTool`, `ComposeDeckTool`, `AuditDeckTool`, `ExportDeckTool` — каждый наследует `Tool`, имеет `name`, `description`, `parameters` (JSON Schema) и `async execute(...) -> ToolResult`; `registry() -> list[Tool]`; `manifest() -> WorkflowManifest` со списком `{kind, name, version, sha256, path}` по каждому скиллу и агенту.

- [ ] **Step 1: Тест версионирования**

```python
# tests/agent/test_versions.py
def test_every_skill_and_agent_declares_a_version():
    for path in [*Path("skills").glob("*/SKILL.md"), *Path("agents").glob("*/AGENT.md")]:
        front = read_frontmatter(path)
        assert re.fullmatch(r"\d+\.\d+\.\d+", front["version"]), path

def test_manifest_hashes_change_when_a_prompt_changes(tmp_path):
    before = manifest()
    path = Path("agents/outline-writer/AGENT.md")
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(original + "\nДополнительное правило.\n", encoding="utf-8")
        assert manifest().sha_for("outline-writer") != before.sha_for("outline-writer")
    finally:
        path.write_text(original, encoding="utf-8")

def test_manifest_is_written_into_every_generated_deck():
    """По готовому файлу должно быть видно, какими версиями скиллов он собран."""
    pptx = build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)
    assert "outline-writer 1.0.0" in read_custom_properties(pptx)["deckforge_workflow"]

def test_no_prompt_text_is_hardcoded_in_python():
    """ТЗ: промпты и конфиги лежат отдельными файлами, не зашиты в код."""
    for path in Path("src/deckforge").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if "Ты " in line or "Ответь" in line or "Правила:" in line:
                assert "AGENT.md" in source or "SKILL.md" in source, f"{path}: похоже на вшитый промпт"
```

- [ ] **Step 2: Вендорить Mini-Agent**

```bash
git clone --depth 1 https://github.com/MiniMax-AI/Mini-Agent.git /tmp/mini-agent
git -C /tmp/mini-agent rev-parse HEAD > vendor/MINI_AGENT_COMMIT
cp -r /tmp/mini-agent/mini_agent vendor/mini_agent
cp /tmp/mini-agent/LICENSE vendor/mini_agent/LICENSE
rm -rf vendor/mini_agent/skills   # чужие скиллы не нужны, свои лежат в skills/
```

Правки форка держать минимальными и перечислить их в `vendor/PATCHES.md`: провайдер переключается на `openai` с `api_base` эндпоинта Yandex, `skills_dir` указывает на `skills/`, добавлена проверка модели через `deckforge.provider.registry`. Больше ничего в форке не менять — иначе обновление апстрима превратится в переписывание.

- [ ] **Step 3: Написать три SKILL.md**

`template-decompose` описывает, что модель может узнать о шаблоне и каким инструментом; `deck-compose` — как писать содержание и что вёрстку делает код; `deck-audit` — как читать отчёт аудита и какие находки чинить, а какие оставить человеку. Тон у `deck-compose` тот же, что показал себя в aiva: «Твоя работа — содержание: что на слайдах и в каком порядке. Вёрстку целиком делает код по разобранному шаблону. Не пиши python-pptx руками, не подбирай цвета, не ставь координаты».

- [ ] **Step 4: Реализовать инструменты и реестр, прогнать, коммит**

Run: `uv run pytest tests/agent/ -v`

```bash
git add vendor skills src/deckforge/agent tests/agent
git commit -m "feat(agent): форк Mini-Agent, скиллы и версионирование воркфлоу"
```

---

### Task 16: API и веб-интерфейс

**Files:**
- Create: `src/deckforge/api/app.py`, `jobs.py`, `schemas.py`
- Create: `web/` (Next.js 15, App Router, TypeScript)
- Test: `tests/api/test_api.py`, `web/e2e/generate.spec.ts`

**Interfaces:**
- `POST /api/templates` — загрузка .pptx, возвращает `template_id` и `TemplateProfile`.
- `GET /api/templates/{id}/profile` — дизайн-система, каталог лейаутов и паттернов для показа.
- `POST /api/decks` — `{template_id, brief, sources, target_slides}`, возвращает `job_id`.
- `GET /api/jobs/{id}` — прогресс по этапам: разбор, структура, текст, вёрстка, аудит, экспорт.
- `GET /api/decks/{id}/variants` — три варианта с превью-PNG и сводкой аудита.
- `POST /api/decks/{id}/fix` — `{variant, finding_ids}`, применяет выбранные исправления и пересобирает.
- `GET /api/decks/{id}/export?format=pptx|pdf|html`.

- [ ] **Step 1: Тест API**

```python
# tests/api/test_api.py
def test_upload_template_returns_profile(client):
    with open("dataset/templates/VK Tech шаблон.pptx", "rb") as fh:
        response = client.post("/api/templates", files={"file": fh})
    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["patterns"]
    assert body["profile"]["palette"]["roles"]["brand"].startswith("#")

def test_generation_reports_progress_by_stage(client, template_id):
    job = client.post("/api/decks", json={"template_id": template_id, "brief": BRIEF}).json()
    stages = _poll_stages(client, job["job_id"])
    assert stages == ["parse", "outline", "write", "compose", "audit", "export"]

def test_three_variants_are_returned(client, deck_id):
    variants = client.get(f"/api/decks/{deck_id}/variants").json()
    assert len(variants) == 3
    assert all(v["preview_pngs"] for v in variants)

def test_fix_applies_only_the_selected_findings(client, deck_id):
    before = client.get(f"/api/decks/{deck_id}/variants").json()[0]
    chosen = [before["findings"][0]["id"]]
    after = client.post(f"/api/decks/{deck_id}/fix", json={"variant": "dense", "finding_ids": chosen}).json()
    assert chosen[0] not in {f["id"] for f in after["findings"]}
    assert len(after["findings"]) == len(before["findings"]) - 1

def test_rejected_template_gives_a_readable_error(client):
    response = client.post("/api/templates", files={"file": ("x.txt", b"not a pptx")})
    assert response.status_code == 400
    assert "pptx" in response.json()["detail"].lower()
```

- [ ] **Step 2: Реализовать API**

Работа идёт в фоне через `asyncio.TaskGroup` с публикацией событий прогресса; клиент читает их через SSE. Артефакты складываются в каталог задания, а не в память.

- [ ] **Step 2a: Починка по умолчанию, выбор по желанию**

ТЗ требует, чтобы пользователь выбирал, какие проблемы чинить. Но демонстрация идёт без диалога: на вход незнакомый шаблон и контент-пакет, на выходе готовая презентация. Поэтому по умолчанию чинится всё, что чинится автоматически, а экран выбора остаётся для случая, когда человек хочет вмешаться. `POST /api/decks` принимает `autofix: bool = True`.

- [ ] **Step 3: Собрать интерфейс**

Четыре экрана: загрузка шаблона с показом разобранной дизайн-системы (палитра с ролями, шкала кеглей, сетка, каталог паттернов); ввод брифа и материалов; сравнение трёх вариантов бок о бок с превью слайдов; экран аудита — превью слайда с подсветкой найденных проблем поверх картинки по координатам finding, чекбокс на каждой находке и кнопка «исправить выбранное».

Подсветка артефактов ровно та, которую требует ТЗ: «система визуализирует найденные проблемы, пользователь выбирает, какие из них исправить».

- [ ] **Step 4: Прогнать e2e, коммит**

Run: `cd web && pnpm build && pnpm test:e2e`

```bash
git commit -am "feat(api,web): API генерации и интерфейс с аудитом и выбором исправлений"
```

---

### Task 17: Документация, фикстуры, проверка на незнакомом шаблоне

**Files:**
- Create: `README.md`, `docs/ARCHITECTURE.md`, `docs/MODELS.md`, `docs/AUDIT.md`
- Create: `fixtures/content-packs/*.md`
- Test: `tests/test_e2e.py`, `tests/test_layer_boundaries.py`

- [ ] **Step 1: Тест границ слоёв**

```python
# tests/test_layer_boundaries.py
def test_parser_does_not_call_the_model():
    """ТЗ оценивает «прозрачное разделение программных слоёв»."""
    for path in Path("src/deckforge/template").rglob("*.py"):
        if path.name == "naming.py":
            continue
        assert "provider" not in path.read_text(encoding="utf-8"), path

def test_composer_does_not_call_the_model():
    for path in Path("src/deckforge/compose").rglob("*.py"):
        assert "provider" not in path.read_text(encoding="utf-8"), path

def test_planner_does_not_touch_pptx():
    for path in Path("src/deckforge/plan").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "pptx" not in source and "Inches" not in source, path
```

- [ ] **Step 2: E2E на шаблоне, который парсер не видел при разработке**

`dataset/templates/ЛЦТ2026 Шаблон презентации.pptx` не используется ни в одном тесте выше и держится как контрольный — он играет роль того самого неизвестного шаблона с защиты.

```python
# tests/test_e2e.py
UNSEEN = Path("dataset/templates/ЛЦТ2026 Шаблон презентации.pptx")

@live
def test_full_pipeline_on_an_unseen_template():
    profile = TemplateProfile.from_file(UNSEEN)
    assert len(profile.patterns) >= 6
    deck = generate(brief=BRIEF, sources=SOURCES, profile=profile, target_slides=12)
    for variant in Variant:
        path = deck.variants[variant].pptx
        errors = [f for f in run_deterministic(path, profile, CONFIG) if f.severity == "error"]
        assert errors == [], f"{variant}: {errors}"

@live
def test_deck_is_generated_within_five_minutes():
    """Жёсткое требование ТЗ."""
    start = time.monotonic()
    generate(brief=BRIEF, sources=SOURCES, profile=TemplateProfile.from_file(UNSEEN), target_slides=12)
    elapsed = time.monotonic() - start
    assert elapsed < 300, f"колода собиралась {elapsed:.0f}с"

@live
def test_nine_decks_for_the_demo():
    """ТЗ, промежуточный этап: один контент, три шаблона, три варианта — девять презентаций."""
    produced = []
    for template in ALL_TEMPLATES:
        profile = TemplateProfile.from_file(template)
        deck = generate(brief=BRIEF, sources=SOURCES, profile=profile, target_slides=12)
        produced.extend(deck.variants.values())
    assert len(produced) >= 9
```

- [ ] **Step 3: Написать четыре документа**

`README` — сетап, переменные окружения, ограничения (в том числе честно: SmartArt строится нативными шейпами, а не форматом `dgm:`; встроенные шрифты требуют установки на машине получателя). `ARCHITECTURE` — пайплайн и границы слоёв, с объяснением, почему парсер детерминированный и почему модель не ставит координаты. `MODELS` — таблица моделей с лицензиями, размерами, ссылками на huggingface и списком отвергнутых с причиной; собирается из `config/models.yaml` скриптом, чтобы не разойтись с кодом. `AUDIT` — 24 детерминированные проверки и 11 модельных, с областью покрытия каждой и обоснованием порогов.

- [ ] **Step 4: Прогнать всё, коммит**

Run: `set -a && . ./.env && set +a && uv run pytest -v`

```bash
git commit -am "docs: README, ARCHITECTURE, MODELS, AUDIT и сквозная проверка на незнакомом шаблоне"
```

---

## Self-review

**Покрытие ТЗ.** Прошёл по разделам.

- п.2.1 парсинг шаблона — Tasks 2–8. п.2.2 генерация структуры и текста по брифу — Task 13. п.2.3 генерация слайдов с контекстной визуализацией — Tasks 9–10. п.2.3.1 text-to-image внутри слайда — **не покрыт**, и это осознанно: задача со звёздочкой для десяти команд, прошедших отбор, и модель до 20B под картинки на доступном ключе отсутствует. Отмечено ниже как явный пробел.
- п. «Функциональность» 4 версионирование скиллов и агентов — Task 15. 5 три варианта вёрстки — Task 13. 6 аудит с визуализацией и выбором исправлений — Tasks 11, 12, 16. 7 экспорт в три формата нативными объектами — Tasks 9, 14.
- Рамки решения: 10–15 слайдов — тест в Task 13; 5 минут — тест в Task 17; независимость от трёх шаблонов — тест на хардкод в Task 8 и контрольный четвёртый шаблон в Task 17.
- п.3 модели только Apache/MIT до 35B — Task 1, проверка в конструкторе провайдера. Браузеры и десктоп — Task 16.
- п.4 документация README/ARCHITECTURE/MODELS/AUDIT — Task 17; промпты отдельными файлами — Task 15, с тестом на вшитые промпты.
- Приложение 1 — Tasks 11 и 12, все 24 + 11 проверок перечислены поимённо.

**Незакрытое, о чём надо сказать вслух.** Генерация изображений внутри слайда (п.3.1) требует text-to-image до 20B; на ключе Yandex такой модели нет. Варианты: локальный SDXL-Turbo или Flux.1-schnell (Apache 2.0) на M4 либо отдельный ключ. До решения слайды используют иконки и фотографии из самого шаблона, что для оценки «встраивание изображений» частично засчитывается, но звёздочку не закрывает.

**Плейсхолдеры.** Прогнал по тексту: «TBD», «позже», «аналогично задаче N» не встречаются. Пороги везде числовые и вынесены в `config/audit.yaml`.

**Согласованность типов.** `Box`, `Canvas`, `Pattern`, `PatternSlot`, `Finding`, `DeckSpec`, `SlideSpec`, `Variant`, `TemplateProfile` определены по одному разу и используются под теми же именами дальше. `measure()` из Task 9 — единственная функция замера, её зовут и `compose`, и `audit`. `run_deterministic` имеет одну сигнатуру во всех задачах.

---

### Task 19: Два настоящих агента вместо пяти одиночных вызовов

Решение принято пользователем 22 сентября 2026 после разбора вопроса «а агент у нас вообще используется».

**Что было.** Пять ролей модели, у каждой промпт файлом с версией, но все — одиночные вызовы: отправили, получили ответ, код проверил. Цикла, где модель видит последствия своего действия и поправляется, нет нигде. Папка называется `agents/`, и по строгому счёту это натяжка.

**Что решили.** Сделать настоящими агентами два места из пяти — те, где цикл даёт качество. Остальные три оставить одиночными вызовами и написать в документации почему, а не делать вид, что не успели.

Разбор по ролям:

| Роль | Агент? | Почему |
|---|---|---|
| Текст слайдов | **да** | Пишет вслепую, не зная, влезет ли текст. С инструментом замера проверит сама и перепишет короче — это чинит самую частую находку аудита |
| Нестандартный слайд | **да** | Сейчас его некуда положить. Модель пишет код вёрстки в песочнице, аудит проверяет, находки возвращаются, модель переписывает |
| Выбор раскладки | нет | Цикл уже есть на коде: положили, проверили, взяли другую. Миллисекунды и полная повторяемость. Модель здесь — регресс |
| Имена ролей палитры | нет | Одно решение по списку цветов, цикл не из чего строить |
| Вопросы аудита | нет | Одиннадцать вопросов, один ответ на слайд |

**Цена по времени.** Агент — это несколько обращений вместо одного. Текст слайдов сейчас 77 секунд на двенадцать слайдов; с циклом в два шага станет порядка двухсот. Общее время вырастет со 104 до примерно 200 секунд при лимите 300. Влезаем, запас съедается. Если станет тесно — писать текст не в четыре потока, а в восемь.

**Инструменты агента, пишущего текст:**
- померить, влезает ли предложенный текст в слот целевой раскладки (использовать `compose.textfit.measure`, единственный замер в проекте);
- проверить, встречается ли число в исходных материалах.

Цикл ограничен двумя шагами. Не уложился — отдаёт что есть, находка остаётся аудиту.

**Песочница нестандартного слайда** — по описанию Task 10a, которая была запланирована и не сделана: `exec` с пустым `__builtins__`, namespace только из примитивов шаблона, жёсткий лимит времени и операций, после исполнения сразу `run_deterministic`, две попытки, потом откат на раскладку с наименьшим переполнением. Код каждого слайда сохраняется в артефакты задания — на защите нужно показать, что именно написал агент.

**Зачем это вообще.** Не ради слова «агент». Пять циклов, три из которых ничего не дают, — это не лучшая оркестрация, а худшая, и эксперт это увидит. Два цикла там, где они дают качество, и честное объяснение по остальным трём — защитимая позиция.

---

### Task 18 (отложена): Генерация изображений внутри слайда

Задача со звёздочкой из п.3.1 ТЗ, для десяти команд, прошедших отборочный этап. Выполняется **после** Tasks 1–17: до отбора она ничего не решает, а пайплайн без неё работает на ассетах шаблона.

**Files:**
- Create: `src/deckforge/provider/imagegen.py`, `src/deckforge/compose/images.py`
- Create: `agents/image-prompter/AGENT.md`
- Modify: `config/models.yaml` (карточка модели), `src/deckforge/template/patterns.py` (слот `image` получает признак «можно генерировать»)
- Test: `tests/compose/test_images.py`

**Ограничение ТЗ:** «Команда предлагает модель класса text-to-image до 20B». На ключе Yandex text-to-image моделей нет, поэтому инференс локальный. Кандидаты, оба подходят по лицензии и размеру: **SDXL-Turbo** (Apache 2.0, 3.5B UNet, один шаг, ~1–2 с на кадр на M4) и **FLUX.1-schnell** (Apache 2.0, 12B, 4 шага, качество выше, ~8–15 с на кадр и ~24 ГБ в fp16, на 16 ГБ требует квантования). Запуск через MLX (`mlx-community/stable-diffusion-xl-turbo`) либо `diffusers` с MPS.

- [ ] **Step 1: Тест**

```python
# tests/compose/test_images.py
def test_generated_image_matches_the_slot_aspect_ratio():
    slot = next(s for s in PATTERN.slots if s.role == "image")
    image = generate_for_slot(slot, "команда за работой в опенспейсе", PROFILE, GEN)
    expected = (slot.box.width * CANVAS.width_in) / (slot.box.height * CANVAS.height_in)
    assert abs(Image.open(image).width / Image.open(image).height - expected) < 0.02

def test_prompt_carries_the_template_palette():
    """Картинка в чужой гамме ломает шаблон сильнее, чем её отсутствие."""
    prompt = build_image_prompt("команда за работой", PROFILE, PROMPTER)
    assert PROFILE.palette.roles["brand"] in prompt or "синий" in prompt.lower()

def test_falls_back_to_template_assets_when_generation_is_unavailable():
    image = generate_for_slot(SLOT, "команда за работой", PROFILE, UnavailableGenerator())
    assert image in {a.part_name for a in PROFILE.assets.photos}

def test_generation_stays_within_the_deck_time_budget():
    """5 минут на колоду — общий бюджет. На картинки отводится не больше 90 секунд,
    дальше слайды добирают ассетами шаблона."""
    start = time.monotonic()
    generate_deck_images(DECK, PROFILE, GEN, budget_s=90)
    assert time.monotonic() - start < 100

def test_image_is_embedded_as_a_picture_shape_not_as_the_whole_slide():
    """ТЗ: слайд, выгруженный единым растровым изображением, не засчитывается."""
    prs = Presentation(str(build_deck(DECK_WITH_IMAGES, PROFILE, TEMPLATE, Variant.visual)))
    for slide in prs.slides:
        assert any(s.shape_type != MSO_SHAPE_TYPE.PICTURE for s in slide.shapes)
```

- [ ] **Step 2: Реализовать**

`imagegen.py` — интерфейс `ImageGenerator.generate(prompt, width, height, seed) -> bytes` с реализацией на MLX и честным отказом, когда модель не скачана; отказ приводит к фолбэку на фотографии шаблона, а не к падению колоды. `agents/image-prompter/AGENT.md` превращает смысл слайда в промпт, вкладывая туда палитру шаблона и запрет на текст внутри картинки (диффузионные модели пишут его с ошибками, а проверка C08 «текст без опечаток» смотрит и на картинку). Генерация идёт параллельно вёрстке, с общим бюджетом времени; кадры кэшируются по хешу промпта.

- [ ] **Step 3: Дописать MODELS.md и коммит**

```bash
git commit -am "feat(images): генерация изображений в слот паттерна локальной моделью"
```
