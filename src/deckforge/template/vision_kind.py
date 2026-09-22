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
незнакомого файла.

## Разбор незнакомого шаблона в бюджет (задача-продолжение task-18)

Живой контрольный замер (`ЛЦТ2026 Шаблон презентации.pptx`, чистый кеш, оба
провайдера, см. task-18-report.md) поднял разбор незнакомого шаблона с 0.8с
до 187.5с — при лимите ТЗ в 300с на генерацию ОДНОЙ презентации целиком (разбор
+ структура + текст + сборка). Три независимые причины, три независимых
починки в этом модуле:

1. **Спрашивалась модель про КАЖДЫЙ паттерн**, включая те, чей вид геометрия
   определила уверенно (ряд из ≥3 карточек с ≥2 элементами в группе — это
   карточки, тут не о чем спорить). Теперь спрашиваются только паттерны с
   `Pattern.kind_confidence < _ASK_CONFIDENCE_THRESHOLD` (см. её докстроку —
   `patterns._classify_kind` размечает уверенность каждой ветви классификации
   отдельно, не эта функция гадает по результату).
2. **Одно обращение — один паттерн.** Теперь до `_BATCH_SIZE` паттернов,
   прошедших порог неуверенности, склеиваются в ОДНУ картинку-сетку
   (`_build_grid_collage`, тот же приём, что уже применён для коллажа
   C09/C11 колоды в `audit/visual.py::_build_collage`, только сеткой
   колонок/строк, а не вертикальной простынёй, и с подписью номера над
   каждой ячейкой, не под ней) и уходят ОДНИМ вызовом `ask_image` с ОДНИМ
   промптом на всю сетку сразу.
3. **Оставшиеся обращения гоняются параллельно** — было и раньше
   (`ThreadPoolExecutor`), но число потоков было хардкод-константой модуля;
   теперь настройка (`config/app.yaml`, `llm.pattern_kind_max_workers`), тем
   же приёмом, что и `plan.writer.DEFAULT_WRITER_MAX_WORKERS` /
   `slide_writer_max_workers`.

Размер пачки (`_BATCH_SIZE`) — НЕ "чем больше, тем быстрее" (та же
оговорка, что уже честно сделана `audit/visual.py` про свой коллаж C09/C11:
"коллаж оказался ТЯЖЕЛЕЕ по бюджету, чем один слайд"). Живой замер этой
задачи (три пачки одних и тех же шести уже классифицированных поодиночке
слайдов VK Education, `max_tokens=16000`, чтобы не путать нехватку бюджета
с реальной стоимостью):

| Размер пачки | prompt_tokens | completion_tokens | finish_reason | Время |
|---|---:|---:|---|---:|
| 2 | 1181 | 2715 | stop | 16.6с |
| 3 | 1676 | 5369 | stop | 31.2с |
| 4 | 1691 | 4047 | stop | 26.0с |

Все три ответили валидно и совпали с результатом одиночных вызовов на тех же
слайдах (cards/table/image/section). Стоимость на ОДИН паттерн внутри пачки
(completion_tokens / размер пачки) не растёт монотонно с размером пачки на
этой маленькой выборке (1358/1790/1012) — не то резкое удорожание, которое
`audit/visual.py` увидел на своих девяти развёрнутых вопросах на коллаж
(там ответ модели — объект с "where" на каждый вопрос, здесь — три токена
`{"kind": "..."}` на паттерн, качественно дешевле). `_BATCH_SIZE = 3` выбран
серединой измеренного диапазона — заметное сокращение числа обращений (в 3
раза меньше, чем без пачек) при заметном ЗАПАСЕ бюджета под пачку (9216,
почти вдвое больше измеренных 5369) и умеренном "блейст-радиусе" отказа
одной пачки (см. ниже про честную деградацию): пачка — ОДИН вызов
`ask_image`, и если модель не ответила валидным JSON на всю пачку разом, ВСЕ
паттерны этой пачки (не один) остаются на геометрическом виде — размер пачки
прямо определяет, сколько паттернов рискует одним неудачным вызовом. Честная
оговорка (тот же принцип, что и у калибровки `_MAX_TOKENS`/`DEFAULT_MAX_
WORKERS` ниже): три пачки на одном шаблоне — не исчерпывающий перебор, а
одна живая точка данных на калибровочное решение; если доля неотвеченных
пачек на защите останется высокой, размер пачки стоит пересмотреть, а не
считать три измерения окончательным ответом.

Стартовый бюджет `_MAX_TOKENS` калиброван тем же живым замером (шесть
ОДИНОЧНЫХ вызовов на реальных слайдах VK Education разного содержания,
`max_tokens=16000`, чтобы увидеть настоящую стоимость успешного ответа, а не
`finish_reason=length` от заниженного потолка):

| Слайд | completion_tokens | finish_reason |
|---|---:|---|
| 1 | 16000 | length (весь бюджет — reasoning, content пуст, НЕ ответил даже на 16000) |
| 7 | 2052 | stop |
| 16 | 2118 | stop |
| 30 | 609 | stop |
| 39 | 566 | stop |
| 47 | 1426 | stop |

Пять из шести успешных ответов уложились в 566-2118 токенов — `_TOKENS_PER_
ITEM = 3072` (та же величина, что уже была откалибрована предыдущей правкой
этого модуля по итогам её собственного живого замера, здесь заново
подтверждена: наибольший успешный ответ, 2118, укладывается с запасом
~45%) остаётся стартовым бюджетом НА ОДИН паттерн пачки; бюджет пачки —
`_TOKENS_PER_ITEM * len(batch)`, растёт вместе с размером пачки, а не
фиксирован заранее (пачка из трёх паттернов реально может обсуждать втрое
больше содержания, чем один). Честная оговорка: слайд 1 не ответил ДАЖЕ на
16000 — весь бюджет ушёл в `reasoning_content` независимо от потолка, и это
НЕ лечится повышением стартового бюджета (эскалация `provider/yandex.py`
тоже не помогла бы — `MAX_TOKENS_BUDGET_CAP=6144` ниже любого разумного
старта этой роли, эскалация от старта выше потолка не делает ни одного шага,
см. её докстроку). Такие паттерны остаются на честной геометрической
деградации — то же поведение, что и у сетевого сбоя/невалидного JSON, не
новый класс отказа.

**Живая находка обязательной проверки этой самой задачи** (не гадание —
второй контрольный шаблон, ЛЦТ2026): при первом прогоне БЕЗ ретрая на пачку
все три пачки этого шаблона не ответили ВАЛИДНО ни разу (0 из 9 уточняемых
паттернов) — бюджет `_TOKENS_PER_ITEM`, откалиброванный по VK Education,
оказался недостаточен для части пачек ЭТОГО шаблона (`finish_reason=length`
у двух пачек из трёх). Раз пачка — бо́льший "блейст-радиус" отказа, чем один
паттерн (см. выше), починка та же, что уже калибрована для той же модели в
`audit/visual.py::_MAX_MODEL_ATTEMPTS` ("ответ недетерминирован, повторный
ПОЛНЫЙ заход часто отвечает содержательно там, где первый не ответил") —
`_ask_batch` теперь делает до `_MAX_MODEL_ATTEMPTS=2` попыток на пачку.
Честная оговорка: это лечит НЕДЕТЕРМИНИРОВАННЫЙ перекос бюджета (та же
пачка на повторе часто отвечает), но не гарантирует ответ на ДЕЙСТВИТЕЛЬНО
неподъёмной для модели пачке (тот же класс, что и "слайд 1 не ответил даже
на 16000" — реальный, не полностью устранимый увеличением бюджета/ретраев,
остаток риска, честно объявленный, не спрятанный)."""
from __future__ import annotations
import io
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

from deckforge.provider.base import VisionProvider
from deckforge.render.soffice import RenderError, to_pngs
from deckforge.template.patterns import Pattern

AGENT_PATH = Path(__file__).resolve().parents[3] / "agents" / "pattern-kind-vision" / "AGENT.md"
KINDS_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "pattern-kinds.yaml"
APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"

# Ниже какой уверенности геометрии (`Pattern.kind_confidence`, см. её
# докстроку в `patterns.py`) паттерн стоит переспросить моделью — задача
# "разбор незнакомого шаблона в бюджет", находка №2 ("Спрашивать модель не
# про все раскладки... Спрашивай только про сомнительные"). 0.5 — тот же
# порог и то же обоснование, что уже применено к `profile._LOW_CONFIDENCE_
# THRESHOLD` (её докстрока: "не наблюдение за тремя файлами, а сама природа
# доли/вероятности: ниже половины источник менее надёжен, чем монетка") —
# `patterns._classify_kind` размечает каждую ветвь классификации отдельно
# (см. её докстроку про конкретные значения по ветвям), эта константа только
# решает, что делать с уже размеченной уверенностью, не подбирает число под
# три учебных файла.
_ASK_CONFIDENCE_THRESHOLD = 0.5

# Сколько паттернов, прошедших порог неуверенности, склеиваются в ОДНУ
# картинку-сетку и уходят ОДНИМ вызовом `ask_image` — см. докстроку модуля
# ("Разбор незнакомого шаблона в бюджет") про живой замер, обосновавший
# именно это число, не наибольшее из проверенных.
_BATCH_SIZE = 3

# Стартовый бюджет `max_tokens` НА ОДИН паттерн пачки — см. докстроку модуля
# про живой замер (шесть одиночных вызовов, 566-2118 успешных токенов).
# Бюджет ОДНОГО вызова (на пачку из `n` паттернов) — `_TOKENS_PER_ITEM * n`.
_TOKENS_PER_ITEM = 3072


def _max_tokens_for_batch(size: int) -> int:
    return _TOKENS_PER_ITEM * max(1, size)


# До скольких ПОЛНЫХ заходов (свежий вызов `ask_image`, не переиспользование
# пустого ответа) на ОДНУ пачку — см. докстроку `classify_patterns_by_vision`/
# `_ask_batch` про живую находку обязательной проверки (ЛЦТ2026: без ретрая
# 0 из 3 пачек ответили валидно). Тот же приём и то же число, что уже
# калибровано `audit/visual.py::_MAX_MODEL_ATTEMPTS` для той же модели —
# "одна повторная попытка закрывает почти все случаи перекоса бюджета в
# reasoning, третья/четвёртая — уже не «бюджета мало», а другая проблема".
_MAX_MODEL_ATTEMPTS = 2


# Сколько раскладок классифицируются ОДНОВРЕМЕННО (пачками, см. выше) — тот
# же порядок величины и то же рассуждение, что у `plan.writer.DEFAULT_
# WRITER_MAX_WORKERS`/`audit.visual.run_visual(max_workers=4)`: одна и та же
# модель (Yandex Cloud) за одним и тем же провайдером, не "чем больше, тем
# быстрее" — запас на сетевые ретраи каждого параллельного вызова важнее
# скорости самого первого разбора (который к тому же кешируется и
# оплачивается один раз на файл, не на каждую генерацию). Задача "разбор
# незнакомого шаблона в бюджет", находка №4: раньше это было хардкод-
# константой (единственным местом настройки), теперь запасной вариант,
# когда `config/app.yaml` недоступен вовсе (`llm.pattern_kind_max_workers`,
# см. `_default_max_workers` ниже) — тот же принцип, что и `_default_cache_
# dir` в `profile.py`.
DEFAULT_MAX_WORKERS = 4


def _default_max_workers() -> int:
    try:
        from deckforge.settings import Settings

        return Settings.load(APP_YAML_PATH).llm.pattern_kind_max_workers
    except Exception:
        return DEFAULT_MAX_WORKERS


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


# --- сетка-коллаж нескольких слайдов-примеров в одну картинку ---------------

# Ширина одной ячейки сетки — тот же порядок величины, что
# `audit/visual.py::_COLLAGE_WIDTH_PX` (480): и модель, и HTTP-клиент обязаны
# уложиться в разумное время одного запроса даже на пачке из `_BATCH_SIZE`
# ячеек (собранная в scratchpad картинка на пачку из 4 слайдов 16:9 при этой
# ширине остаётся в единицах мегабайт после base64, см. `ask_image`,
# `provider/yandex.py`).
_CELL_WIDTH_PX = 480
_LABEL_HEIGHT_PX = 20
_GRID_COLUMNS = 2


def _build_grid_collage(pngs: list[bytes]) -> bytes:
    """Одна картинка — сетка уменьшенных превью нескольких слайдов-примеров,
    каждый подписан "Слайд N" (1-based, порядок в `pngs`, НЕ номер слайда
    исходного файла — сопоставление с ответом идёт по этому же числу, не по
    оригинальной нумерации, см. `agents/pattern-kind-vision/AGENT.md`).
    Раскладка — `_GRID_COLUMNS` колонок, столько строк, сколько нужно;
    сеткой (не вертикальной простынёй, как `audit/visual.py::_build_
    collage`), как прямо просит бриф задачи ("Склей несколько слайдов-
    примеров в одну картинку СЕТКОЙ")."""
    thumbs: list[Image.Image] = []
    for png in pngs:
        with Image.open(io.BytesIO(png)) as im:
            im = im.convert("RGB")
            ratio = _CELL_WIDTH_PX / im.width
            thumbs.append(im.resize((_CELL_WIDTH_PX, max(1, round(im.height * ratio)))))

    columns = min(_GRID_COLUMNS, len(thumbs)) or 1
    rows = (len(thumbs) + columns - 1) // columns
    cell_height = max((t.height for t in thumbs), default=1) + _LABEL_HEIGHT_PX
    grid = Image.new("RGB", (_CELL_WIDTH_PX * columns, cell_height * rows), color=(255, 255, 255))
    draw = ImageDraw.Draw(grid)
    font = ImageFont.load_default()

    for i, thumb in enumerate(thumbs):
        col, row = i % columns, i // columns
        x, y = col * _CELL_WIDTH_PX, row * cell_height
        draw.rectangle([x, y, x + _CELL_WIDTH_PX, y + _LABEL_HEIGHT_PX], fill=(0, 0, 0))
        draw.text((x + 4, y + 3), f"Слайд {i + 1}", fill=(255, 255, 255), font=font)
        grid.paste(thumb, (x, y + _LABEL_HEIGHT_PX))

    buf = io.BytesIO()
    grid.save(buf, format="PNG")
    return buf.getvalue()


def classify_patterns_by_vision(
    patterns: list[Pattern],
    template_path: Path,
    llm: VisionProvider | None,
    *,
    max_workers: int | None = None,
) -> tuple[list[Pattern], list[str]]:
    """Уточняет `Pattern.kind` паттернов, чью геометрию `patterns._classify_
    kind` разметила НЕУВЕРЕННОЙ (`kind_confidence < _ASK_CONFIDENCE_
    THRESHOLD` — см. докстроку модуля, находка №2), показом модели картинки
    ТЕХ слайдов-примеров, с которых они сняты (`Pattern.source_slide_index[0]`,
    тот же слайд, что и обоснование самого паттерна в отчёте — первый
    источник, когда паттернов, снятых с разных, но геометрически совпавших
    слайдов, несколько, см. `patterns._dedup`), пачками до `_BATCH_SIZE`
    штук за один вызов `ask_image` (докстрока модуля, находка №3).

    `max_workers` — сколько пачек классифицируются ОДНОВРЕМЕННО; `None`
    (по умолчанию) читает `config/app.yaml` (`llm.pattern_kind_max_workers`,
    см. `_default_max_workers`), тем же принципом, что и `profile.
    _default_cache_dir` — явно переданное число (в т.ч. тестами) не
    подменяется конфигом.

    Возвращает (новый список паттернов, заметки для `TemplateProfile.
    provenance`/`.warnings`) — НИКОГДА не бросает исключение наружу: сбой
    рендера, сети или разбора ответа модели превращает соответствующие
    паттерны в "оставлены геометрическим предположением", не в упавший
    разбор профиля (тот же принцип честной деградации, что у
    `naming.name_palette_roles_report`). Отказ ОДНОЙ ПАЧКИ (сеть/невалидный
    JSON) откатывает ВСЕ паттерны этой пачки к геометрическому виду, не
    только один — прямое следствие того, что пачка это один вызов на
    несколько паттернов разом (докстрока модуля про "блейст-радиус")."""
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

    # Находка №2: геометрически уверенные паттерны модель вообще не видит —
    # ни рендера, ни сетевого вызова на них не тратится.
    uncertain = [p for p in patterns if p.kind_confidence < _ASK_CONFIDENCE_THRESHOLD]
    certain_count = len(patterns) - len(uncertain)
    if not uncertain:
        return list(patterns), [
            f"Виды раскладки: все {len(patterns)} паттернов геометрия определила уверенно "
            f"(kind_confidence >= {_ASK_CONFIDENCE_THRESHOLD}) — модель не спрашивалась ни разу."
        ]

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

        batches: list[list[Pattern]] = [
            uncertain[i : i + _BATCH_SIZE] for i in range(0, len(uncertain), _BATCH_SIZE)
        ]

        reclassified: dict[str, str] = {}  # pattern_id -> новый kind
        skip_notes: list[str] = []
        calls = 0
        calls_answered = 0

        def _ask_batch(batch: list[Pattern]) -> tuple[dict[str, str], list[str], bool, bool]:
            """Возвращает (pattern_id -> новый_kind для успешно уточнённых,
            заметки об отказе отдельных паттернов пачки, действительно_ли_
            звонили_модели (для счётчика обращений — пачка, где НИ у одного
            паттерна не нашлось рендера слайда-источника, сеть не трогает
            вовсе), отвечала_ли_модель хоть чем-то валидным на эту пачку
            целиком)."""
            items = []
            grid_pngs: list[bytes] = []
            # Позиция в сетке (1-based, `i["index"]`) назначается только
            # паттернам, у которых нашёлся рендер слайда-источника —
            # `position_to_pattern` строится ТУТ ЖЕ, по факту добавления в
            # `items`/`grid_pngs`, а не восстанавливается задним числом по
            # `batch`: так позиция каждого паттерна в ответе модели однозначна,
            # даже если часть паттернов пачки пропущена (см. `missing_notes`
            # ниже) и порядковые номера `items` не совпадают 1-в-1 с индексами
            # `batch`.
            position_to_pattern: dict[int, Pattern] = {}
            missing_notes: list[str] = []
            for pattern in batch:
                slide_no = pattern.source_slide_index[0] if pattern.source_slide_index else None
                png = png_by_slide.get(slide_no) if slide_no is not None else None
                if png is None:
                    missing_notes.append(
                        f"{pattern.pattern_id}: слайд-источник {slide_no} не нашёлся среди отрисованных "
                        f"страниц — вид оставлен геометрическим ({pattern.kind})."
                    )
                    continue
                position = len(items) + 1
                items.append({"index": position, "geometric_hint": pattern.kind})
                grid_pngs.append(png)
                position_to_pattern[position] = pattern

            if not items:
                return {}, missing_notes, False, False

            grid_png = _build_grid_collage(grid_pngs)
            payload = {"kinds": kinds, "items": items}
            prompt = f"{agent_body}\n\nСлужебные данные:\n{json.dumps(payload, ensure_ascii=False)}"
            max_tokens = _max_tokens_for_batch(len(items))

            # До `_MAX_MODEL_ATTEMPTS` ПОЛНЫХ заходов (свежий вызов `ask_
            # image`, не переиспользование пустого ответа) — тот же приём и
            # то же обоснование, что `audit/visual.py::_ask_and_parse_with_
            # retry` (её докстрока: "ответ недетерминирован... повторный
            # запрос часто отвечает содержательно там, где первый не
            # ответил"). Найдено ЖИВЫМ прогоном этой самой задачи (обяза-
            # тельная проверка, ЛЦТ2026): без ретрая все 3 пачки этого
            # шаблона не ответили ВАЛИДНО ни разу (0 из 9 уточнённых) — две
            # ушли в finish_reason=length на бюджете, рассчитанном по VK
            # Education, третья вернула пустую строку. Пачка — бо́льший
            # блейст-радиус отказа, чем один паттерн (докстрока модуля),
            # поэтому вторая попытка здесь важнее, чем была бы для одиночного
            # вызова.
            attempt_exc: Exception | None = None
            answer: dict | None = None
            for _attempt in range(_MAX_MODEL_ATTEMPTS):
                try:
                    raw = llm.ask_image(grid_png, prompt, max_tokens=max_tokens)
                    parsed = json.loads(raw)
                except Exception as exc:  # noqa: BLE001 — сеть/парсинг пробуем ещё раз, не роняем разбор профиля
                    attempt_exc = exc
                    continue
                if not isinstance(parsed, dict):
                    attempt_exc = TypeError(
                        f"модель вернула {type(parsed).__name__}, ожидался объект JSON"
                    )
                    continue
                answer = parsed
                attempt_exc = None
                break

            if answer is None:
                notes = list(missing_notes)
                notes.append(
                    f"пачка из {len(items)} паттернов ({', '.join(p.pattern_id for p in batch)}): "
                    f"вид моделью не получен после {_MAX_MODEL_ATTEMPTS} попыток ({attempt_exc}) — "
                    "оставлены геометрическими."
                )
                return {}, notes, True, False

            result: dict[str, str] = {}
            notes = list(missing_notes)
            for pos, pattern in position_to_pattern.items():
                entry = answer.get(str(pos))
                if not isinstance(entry, dict):
                    notes.append(
                        f"{pattern.pattern_id}: модель не ответила про ячейку {pos} пачки — "
                        f"оставлен геометрическим ({pattern.kind})."
                    )
                    continue
                proposed = entry.get("kind")
                if not isinstance(proposed, str) or proposed not in allowed:
                    notes.append(
                        f"{pattern.pattern_id}: модель предложила вид {proposed!r}, которого нет в "
                        f"config/pattern-kinds.yaml — оставлен геометрическим ({pattern.kind})."
                    )
                    continue
                result[pattern.pattern_id] = proposed
            return result, notes, True, True

        with ThreadPoolExecutor(max_workers=max(1, max_workers if max_workers is not None else _default_max_workers())) as pool:
            futures = [pool.submit(_ask_batch, batch) for batch in batches]
            for future in as_completed(futures):
                batch_result, batch_notes, attempted, answered = future.result()
                if attempted:
                    calls += 1
                if answered:
                    calls_answered += 1
                reclassified.update(batch_result)
                skip_notes.extend(batch_notes)

    changed = sum(1 for p in patterns if reclassified.get(p.pattern_id) not in (None, p.kind))
    result = [
        replace(p, kind=reclassified[p.pattern_id]) if p.pattern_id in reclassified else p
        for p in patterns
    ]

    notes = [
        f"Вид раскладки: {len(patterns)} паттернов всего, геометрия уверена (kind_confidence >= "
        f"{_ASK_CONFIDENCE_THRESHOLD}) у {certain_count} без обращения к модели; спрошено про "
        f"оставшиеся {len(uncertain)} пачками до {_BATCH_SIZE} штук — обращений к модели: {calls}, "
        f"из них ответили валидно: {calls_answered} — уточнён вид у {changed} из {len(uncertain)}."
    ]
    notes.extend(sorted(skip_notes))
    return result, notes
