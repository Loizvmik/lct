"""Майнинг композиционных раскладок со слайдов-примеров шаблона.

Плейсхолдеров в шаблонах почти нет (доля контента, привязанного к
плейсхолдерам: 4.8% у VK Tech, 11.2% у WorkSpace, 24.1% у Education — бриф
задачи), поэтому раскладку для генератора приходится не читать из макета
готовой, а СНИМАТЬ с уже собранных человеком слайдов-примеров: найти, что на
слайде текст/картинка/таблица (контент), что — декоративная подложка
композиции (карточки-плашки без текста, линии, логотип), приписать
контентным шейпам роль (`PatternSlot.role`) и, если несколько шейпов
образуют регулярный ряд/сетку (шесть карточек с одинаковым размером и
шагом), свернуть их в один параметрический `RepeatSpec` — это и превращает
шесть нарисованных карточек в раскладку на произвольное число карточек.

Единица майнинга — ОДИН СЛАЙД: брифом (Step 2, п.4) повтор ищется "шейпы с
одинаковой ролью... сворачиваются в RepeatSpec" внутри уже собранной
композиции слайда, не между слайдами. Один слайд даёт максимум один
`Pattern`-кандидат; кандидаты с совпадающим `kind` и близкими боксами слотов
схлопываются на шаге дедупликации (`_dedup`, брифом Step 2, п.8).

Модуль полностью детерминированный — не импортирует `deckforge.provider` и
не обращается к модели (архитектурная граница задачи, бриф "Что уже
готово"). Это остаётся верным и после Task 18: вид раскладки (`Pattern.
kind`, снятый здесь чисто геометрией — семь корзин `_classify_kind` ниже)
может быть уточнён МУЛЬТИМОДАЛЬНОЙ моделью, но не отсюда — уточнение живёт
отдельным модулем-проходом ПОВЕРХ уже намайненных этим модулем `Pattern`
(`template.vision_kind.classify_patterns_by_vision`, вызывается из
`profile.py::TemplateProfile.from_file`), не внутри `mine_patterns`. Тот же
принцип, что и с `naming.py` — `patterns.py` производит честный, проверяемый
геометрический результат независимо от того, есть ли ключ модели; что этим
результатом делает вызывающий код (в т.ч. заменяет `kind` предложением
модели) — забота вызывающего, не этого модуля.

Ключевое ограничение, честно заявленное в отчёте задачи, не спрятанное в
коде: `estimate_slot_chars` ниже — площадная эвристика ("сколько знаков
влезет в рамку" по средней ширине символа и высоте строки в em), а не замер
глифов реального шрифта. Полноценный замер (модуль `textfit`) — Task 9;
оценка вынесена в отдельную функцию именно затем, чтобы её можно было
заменить одной правкой без изменения остального пайплайна (брифом,
"Требования к работе").
"""
from __future__ import annotations
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image

from deckforge.ooxml.color import Color, resolve_color
from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import local_name, qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import ShapeRef, walk_shapes
from deckforge.template.assets import AssetCatalog
from deckforge.template.grid import TITLE_PH_TYPES, Grid, cluster
# Task 7 повторное ревью, находка №4 ("Определение тёмного фона слабее, чем
# уже есть в проекте"): `layouts.py` уже умеет резолвить `p:bg` (цепочка
# слайд→лейаут→мастер→lt1), градиенты (цвет первой точки) и яркость
# картиночного фона (пиксельный сэмпл) — сильнее декоративно-фигурной
# эвристики, которая была здесь раньше (см. `_slide_is_dark`). Вместо
# повторной реализации той же логики — прямой импорт её приватных хелперов
# из `layouts.py` (осознанное отступление от заведённого в template/
# принципа "каждый модуль читает свой срез XML сам, не тянет чужие
# приватные функции" — здесь наоборот прямо потребовало код-ревью: одна и
# та же p:bg-цепочка не должна разъезжаться в двух местах).
from deckforge.template import layouts as _layouts
from deckforge.template.theme import ThemeInfo, pick_primary_master, read_theme
from deckforge.template.typography import TypeScale

# --- контракт данных (бриф, интерфейс Task 7) -------------------------------

ROLES = frozenset({
    "headline", "subhead", "body", "bullet", "card_title", "card_body",
    "kpi_value", "kpi_label", "quote", "caption", "source", "image", "icon",
    "chart", "table",
})

# Роли, не несущие текста как такового — их max_chars/wraps не имеют смысла
# (см. _build_slot): вместимость текстового слота считается только там, где
# действительно есть текст, который надо будет уместить.
_NON_TEXT_ROLES = frozenset({"image", "icon", "table", "chart"})

KINDS = ("cards", "two_col", "kpi", "section", "image", "table", "bullets")

# Kind'ы, для которых допустимо не иметь слота headline (бриф, тест
# `test_every_pattern_has_a_headline_slot_or_is_marked_decorative`): "section"
# — героический заголовок сам по себе headline (см. _classify_kind), "image"
# — слайд, где смысл несёт картинка, а не текстовая иерархия. Для остальных
# kind'ов отсутствие headline — брак раскладки (см. `_mine_slide`: такой
# кандидат отбрасывается на шаге оценки, а не просачивается без роли).
_HEADLINE_EXEMPT_KINDS = frozenset({"section", "image"})


@dataclass(frozen=True)
class PatternSlot:
    """Один параметрический слот раскладки — место под один кусок будущего
    контента.

    `sample_text` — текст-рыба, снятая с исходного слайда-примера (см.
    докстроку модуля про "Заголовок"/"Текст"/"Имя Фамилия") — НЕ входит в
    контракт интерфейса брифа дословно, добавлено по требованию задачи
    ("вынеси... в отдельную функцию" про capacity плюс прямой тест брифа
    `test_soft_line_break_does_not_corrupt_slot_text`, обращающийся к
    `slot.sample_text`) — тот же принцип, что и добавление `step_confidence`
    к `TypeScale`/`family_variants` в Task 4: поле сверх литерального
    перечня брифа, но без него часть требований задачи физически нечем
    удовлетворить. `None` для нетекстовых ролей (`_NON_TEXT_ROLES`).
    """
    role: str
    box: Box
    size_pt: float
    color_hex: str | None
    align: str
    max_chars: int
    wraps: bool
    sample_text: str | None = None


@dataclass(frozen=True)
class RepeatSpec:
    axis: str  # "x" | "y"
    count: int
    step: float
    slot_roles: list[str] = field(default_factory=list)
    # Число слотов, слитых в ОДНУ повторяющуюся единицу — карточку/строку
    # (заголовок карточки + тело карточки = 2, одна строка списка = 1) — см.
    # `_find_repeat` (там же `len(best)`, число смёрженных кандидатов одной
    # физической сетки). Task 7 повторное ревью №2, находка №1: структурный
    # признак "карточки" (несколько элементов в каждой повторяющейся
    # группе), в отличие от `slot_roles`, не может случайно схлопнуться в
    # единицу, когда несколько элементов группы получили ОДНУ И ТУ ЖЕ роль
    # (пять карточек участников команды, где имя/роль/ник набраны одним
    # кеглем — `slot_roles` был бы {"card_body"}, притом что элементов в
    # группе два и больше).
    group_size: int = 1


@dataclass(frozen=True)
class Capacity:
    max_items: int
    max_chars_per_item: int
    max_bullets: int
    max_series: int
    max_rows: int
    max_cols: int


@dataclass(frozen=True)
class DecorShape:
    """Декоративный шейп раскладки — плашка, линия, иконка без текста,
    логотип и т.п. — переносится в собранный слайд как есть (бриф,
    "Требования к работе": "декор раскладки переноси как есть").

    Хранит не сам `lxml`-элемент (он живёт в дереве конкретного открытого
    `.pptx`-пакета и не переживёт закрытие `PptxPackage`), а достаточно
    геометрии/оформления, чтобы сборщик слайдов мог воспроизвести шейп на
    новом холсте: тип, бокс, поворот/отражение, цвет заливки (если
    разрешился) и была ли заливка вовсе.

    `fill_kind` (Task 7 повторное ревью, находка №6, "мелочи") — вид
    заливки как она объявлена в XML: `"none"` (`noFill`),
    `"unspecified"` (в `p:spPr` вовсе нет узла заливки — наследуется от
    стиля), `"solid"`, `"gradient"`, `"pattern"`, `"picture"` (`blipFill`),
    `"group"` (`grpFill`, наследует заливку родительской группы — из
    контекста этого шейпа она не резолвится). Раньше (до этой правки)
    сборщик получал для градиентной/узорной/картиночной плашки только
    `has_fill=True, fill_hex=None` — "заливка есть, а какая, не знаю" — и
    не мог отличить "забыли посчитать цвет" от "цвета и не может быть
    (картинка)". `fill_hex` теперь заполняется, когда это возможно, ДЛЯ
    ВСЕХ видов заливки, не только сплошной — средним цветом (см.
    `_to_decor`): для градиента — среднее резолвленных цветов всех
    стоп-точек, для узора — среднее переднего/заднего цвета, для картинки —
    среднее пикселей уменьшенной копии (тот же приём, что
    `layouts._picture_luminance`, только цвет, а не яркость).

    `repeat_group`/`repeat_index` (Task 9 повторное ревью, находка №1,
    "главная находка"): принадлежит ли декоративный шейп ТОЙ ЖЕ
    повторяющейся сетке, что и `Pattern.repeat` текстовых слотов — шесть
    одинаковых карточных плашек под шестью карточками текста это не шесть
    независимых украшений, а ОДИН декоративный элемент, повторённый шесть
    раз (см. `_decor_repeat_membership`). Без этого признака `compose/`
    физически нечем отличить "эта плашка — часть повтора, разверни её
    вместе с текстом" от "эта плашка — самостоятельный элемент дизайна,
    переноси как есть": раньше (до этой правки) ВЕСЬ декор переносился
    статически, независимо от фактического числа элементов содержания —
    раскладка, снятая с шести карточек, под два элемента содержания
    оставляла четыре пустые рамки на слайде. `repeat_index` — позиция
    декора внутри группы (0-based, по возрастанию координаты вдоль оси
    повтора), тот же приём, что `expand_repeat` использует для текстовых
    слотов. `False`/`0` по умолчанию — декор вне группы повтора (логотип,
    разделительная линия, самостоятельная плашка), переносится КАК ЕСТЬ.

    `prst`/`adj` (Task 10 код-ревью, находка №1): `a:prstGeom`/@prst и
    степень скругления (`a:avLst` `adj`) декора — независимая копия того
    же разбора, что `template/shapes.py::_prst_geom` (не импорт чужой
    приватной функции, тот же принцип модульной границы, что уже
    применён во всём `template/`, см. докстроку модуля про `_layouts`).
    Заполняются ТОЛЬКО для `kind == "shape"` (`p:sp` несёт `a:prstGeom`;
    коннектор/картинка/graphicFrame — нет). Нужны, чтобы `build_shape_
    vocabulary` могло строить словарь автофигур ИМЕННО по декору групп
    повтора (карточные плашки), а не по переписи всех автофигур шаблона
    (см. докстроку `template/shapes.py`) — без формы у самого декора
    опираться было бы физически не на что. `None` — декор не несёт
    `a:prstGeom` (не `kind == "shape"`, либо `p:custGeom` — произвольный
    контур, ни один известный прест ему не соответствует)."""
    kind: str
    box: Box
    rotation: float
    flip_h: bool
    flip_v: bool
    fill_hex: str | None
    has_fill: bool
    fill_kind: str = "unspecified"
    repeat_group: bool = False
    repeat_index: int = 0
    prst: str | None = None
    adj: float | None = None


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    # Список, не одно число (бриф Step 2, п.8: "source_slide_index
    # накапливается" при дедупликации совпавших по kind/геометрии
    # кандидатов с разных слайдов) — раскладка, снятая с нескольких похожих
    # слайдов, честно показывает все свои источники, а не только первый
    # найденный (нужно отчёту о разборе, который читает человек — бриф,
    # "Требования к работе").
    source_slide_index: list[int]
    layout_id: str
    kind: str
    slots: list[PatternSlot]
    repeat: RepeatSpec | None
    decor: list[DecorShape]
    capacity: Capacity
    score: float
    is_dark: bool
    # Уверенность ГЕОМЕТРИЧЕСКОГО классификатора (`_classify_kind` ниже) в
    # своём выборе `kind` — НЕ то же самое, что `score` (пригодность
    # паттерна к повторному использованию вообще). Нужна отдельному проходу
    # `template.vision_kind.classify_patterns_by_vision`: находка задачи
    # ("Спрашивать модель не про все раскладки") — геометрия уверена, когда
    # структурный сигнал сам по себе однозначен (повтор ≥3 групп по ≥2
    # элемента — это карточки, настоящая `<a:tbl>` — это таблица), и
    # колеблется там, где несколько РАЗНЫХ смысловых видов геометрически
    # неотличимы (headline-only слайд может быть и section, и quote, и
    # kpi_caption — см. `config/pattern-kinds.yaml`). Значения по ветвям
    # `_classify_kind`, каждое обосновано в её докстроке; дефолт `1.0`
    # (обратная совместимость со старым диск-кешем/тестовыми фикстурами,
    # не задающими поле явно, — то же поведение, что и у `RepeatSpec.
    # group_size`/`DecorShape.repeat_group` в этом же модуле, см. их
    # докстроки про "кеш без поля валидируется как..." принцип).
    kind_confidence: float = 1.0


# --- геометрические допуски (бриф, Step 2, п.4 — оба числа литералом) ------

# "совпадающим размером (допуск 2%)" — доля холста, тот же принцип единиц,
# что и _CLUSTER_TOLERANCE в grid.py (абсолютная доля, не относительная
# к размеру самого шейпа): 2% ширины/высоты холста — заметно больше
# погрешности округления EMU, но меньше типичной разницы между двумя
# ДЕЙСТВИТЕЛЬНО разными по размеру слотами.
_SIZE_TOLERANCE = 0.02

# "равным шагом по одной оси (допуск 0.5% холста)" — брифом дословно.
_STEP_TOLERANCE = 0.005

_MIN_REPEAT_COUNT = 2

# --- пороги оценки качества (см. _score) — обоснование при каждой константе --

# Минимальный score, ниже которого паттерн отбрасывается (бриф: "раскладка,
# снятая плохо, хуже её отсутствия"). Ниже трети шкалы 0..1: headline и
# соответствие полям уже гарантированы отдельными жёсткими проверками ДО
# этого порога (см. _mine_slide) — сам `_score` дальше ранжирует ТОЛЬКО
# такие, уже прошедшие структурный минимум, кандидаты по богатству ролей,
# параметричности (repeat) и доле текста-рыбы, поэтому порог не обязан
# быть высоким: он отсекает бедные единичным headline-без-остального
# раскладки, а не судит уже отфильтрованную структурно годную композицию.
# Калибровочная величина, честно объявленная (бриф: "где порог всё же
# приходится калибровать, скажи об этом прямо") — не выведена из трёх
# учебных файлов реверс-инжинирингом, а выбрана как "заметно ниже
# середины шкалы", раз содержательные пороги качества уже отработали
# раньше.
_MIN_SCORE = 0.35

# "Богатая" по числу ролей раскладка — дальше рост числа ролей не должен
# давать прироста role_score (см. _score): 4 различные роли — уже сложная
# многосоставная композиция (заголовок + минимум три разных смысловых
# блока), не откалибровано по трём файлам, а по тому же порядку величины,
# что и число ролей закрытого перечня (бриф, интерфейс, 15 ролей всего).
_ROLE_RICHNESS_CEILING = 4

# Доля площади холста, занятая картинкой, — граница kind="image" (бриф
# Step 2, п.5, литерал).
_IMAGE_AREA_KIND_THRESHOLD = 0.40

# "Заметный отступ от текста-рыбы шаблона" — слова из известной рыбы трёх
# учебных файлов (бриф "Что установила разведка", п.5) плюс типографские
# lorem/placeholder-маркеры общего назначения — используется только как
# МЯГКИЙ штраф в score (см. _score), не как фильтр: почти весь текст на
# слайдах-примерах — рыба по построению задачи (сами слайды рисовал дизайнер
# шаблона, не заказчик), отбрасывать паттерны только за это значило бы не
# намайнить вообще ничего.
_FISH_MARKERS = (
    "заголовок", "текст", "имя фамилия", "должность", "заполнител",
    "lorem", "ipsum", "placeholder", "пример текста",
)

# Порог "область у нижнего края слайда" для caption/source (см.
# _finalize_roles) — доля высоты холста. 8% — заметно уже типичной высоты
# контентного блока, но достаточно, чтобы поймать подпись/копирайт у самого
# низа; округлая отсечка, не выведенная из трёх файлов (тот же принцип, что
# и _EDGE_BAND в assets.py).
_BOTTOM_BAND = 0.08

# Порог "декоративная плашка достаточно велика, чтобы её цвет считать фоном
# слайда" (см. _slide_is_dark) — половина площади холста, округлая отсечка
# (не откалибровано по трём файлам: для оценки is_dark, в отличие от
# Background.luminance в layouts.py, нет отдельного источника p:bg на
# каждом слайде-примере — оценка по крупнейшей декоративной фигуре честнее,
# чем гадать "фон не задан").
_BG_AREA_SHARE = 0.5

_WCAG_DARK_THRESHOLD = 0.5

_NUMERIC_RE = re.compile(r"^[+-]?\d[\d\s.,]{0,7}[%xXхХкKмMмлрдтыс+]{0,3}$")

_BG_FILL_TAGS = frozenset({"noFill", "solidFill", "gradFill", "grpFill", "pattFill", "blipFill"})


# --- вход ---------------------------------------------------------------


def mine_patterns(
    pkg: PptxPackage, canvas: Canvas, grid: Grid, scale: TypeScale, assets: AssetCatalog,
) -> list[Pattern]:
    """Снимает композиционные раскладки со всех слайдов-примеров шаблона.

    Алгоритм на каждый слайд (бриф, Step 2, п.1-7) — см. `_mine_slide`;
    дедупликация результатов по всем слайдам сразу (п.8) — см. `_dedup`.
    """
    theme = read_theme(pkg, pick_primary_master(pkg))
    logo_target = assets.logo.part_name if assets.logo else None
    bg_targets = {ref.part_name for ref in assets.backgrounds}
    # Кэш decode картинки-фона на весь прогон mine_patterns (находка №4):
    # тот же приём и тот же смысл, что `image_luminance_cache` в
    # `layouts.build_layout_catalog` — несколько слайдов нередко ссылаются
    # на одну и ту же картинку-фон, декодировать её заново на каждый слайд
    # не нужно.
    bg_image_cache: dict[str, float | None] = {}
    # Кэш средних цветов картиночных/узорных декоративных заливок (находка
    # №6, "мелочи") — отдельный от `bg_image_cache`: разные части
    # (декоративный шейп со своей картинкой-заливкой vs фон слайда/лейаута)
    # обычно разные медиа-файлы, но даже при совпадении имени части смысл
    # закэшированного значения разный (яркость vs хекс среднего цвета).
    decor_image_cache: dict[str, str | None] = {}

    candidates: list[Pattern] = []
    for slide_part in _slide_parts(pkg):
        pattern = _mine_slide(
            pkg, canvas, grid, scale, theme, slide_part, logo_target, bg_targets,
            bg_image_cache, decor_image_cache,
        )
        if pattern is not None:
            candidates.append(pattern)
    return _dedup(candidates)


def _slide_parts(pkg: PptxPackage) -> list[str]:
    return sorted(n for n in pkg.names() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))


_SLIDE_NUMBER_RE = re.compile(r"slide(\d+)\.xml$")


def _slide_number(part_name: str) -> int:
    """Номер слайда из имени части — для `source_slide_index` (человеку в
    отчёте нужен номер слайда, не позицию в отсортированном по имени
    списке файлов: `sorted()` по строке даёт slide1, slide10, slide2, ...,
    что не совпадает с тем, что видит человек в PowerPoint)."""
    m = _SLIDE_NUMBER_RE.search(part_name)
    return int(m.group(1)) if m else -1


# --- одна раскладка с одного слайда --------------------------------------


@dataclass(frozen=True)
class _TierInfo:
    """Типографический разбор одного контентного текстового шейпа —
    промежуточное сырьё для назначения роли (см. `_finalize_roles`).

    `text` — `None` для пустого плейсхолдера-слота (Task 7 повторное
    ревью, находка №1): нечего измерять/показывать как текст-рыбу, но сам
    слот — не менее реальный кандидат в headline/card_title и т.п., чем
    заполненный (см. `_tier_info`/`_split_content_decor`)."""
    step: str  # ближайшая ступень TypeScale.steps: micro/caption/body/h2/h1/display
    size_pt: float
    numeric: bool
    bulleted: bool
    text: str | None
    align: str
    color_hex: str | None


_TIER_ORDER = ("micro", "caption", "body", "h2", "h1", "display")
_TIER_RANK = {name: i for i, name in enumerate(_TIER_ORDER)}


def _mine_slide(
    pkg: PptxPackage, canvas: Canvas, grid: Grid, scale: TypeScale, theme: ThemeInfo,
    slide_part: str, logo_target: str | None, bg_targets: set[str],
    bg_image_cache: dict[str, float | None], decor_image_cache: dict[str, str | None],
) -> Pattern | None:
    root = pkg.xml(slide_part)
    layout_part = _slide_layout_part(pkg, slide_part)

    refs = list(walk_shapes(root, canvas, include_groups=False))
    refs = _resolve_slide_boxes(pkg, refs, layout_part, canvas)
    refs = [r for r in refs if _visible(r)]
    if not refs:
        return None

    rels = pkg.rels(slide_part)
    content, decor = _split_content_decor(refs, rels, logo_target, bg_targets)
    if not content:
        return None

    tiers = [_tier_info(ref, canvas, scale, theme) for ref in content]

    repeat, repeat_roles_by_index = _find_repeat(content, tiers)
    slots = _finalize_roles(content, tiers, repeat_roles_by_index, canvas, scale)

    if repeat is not None:
        # slot_roles достраивается ФИНАЛЬНЫМИ ролями (не предварительными
        # `_prelim_repeat_role`) до классификации kind — `_classify_kind`
        # ("cards") смотрит именно на `repeat.slot_roles`, поэтому порядок
        # здесь важен: сначала роли, потом kind.
        repeat = replace(repeat, slot_roles=sorted({slots[i].role for i in repeat_roles_by_index}))

    roles_present = {s.role for s in slots}
    kind, kind_confidence = _classify_kind(content, slots, repeat, roles_present, canvas)

    if "headline" not in roles_present and kind not in _HEADLINE_EXEMPT_KINDS:
        # Раскладка без заголовка (и не героического типа section/image) —
        # неполная композиция, майнить нечего (бриф: "раскладка, снятая
        # плохо, хуже её отсутствия"). Гарантирует тестовый контракт
        # `test_every_pattern_has_a_headline_slot_or_is_marked_decorative`
        # СТРУКТУРНО, не совпадением на трёх учебных файлах.
        return None

    slots = _snap_to_margins(slots, grid)
    if _slots_overlap(slots) or not _within_margins(slots, grid):
        return None

    slide_number = _slide_number(slide_part)
    layout_id = Path(layout_part).stem if layout_part else ""
    is_dark = _slide_is_dark(pkg, theme, slide_part, layout_part, decor, bg_image_cache)
    capacity = _capacity(content, slots, repeat, grid, canvas)
    score = _score(slots, repeat, roles_present)
    if score < _MIN_SCORE:
        return None

    decor_membership = _decor_repeat_membership(decor, repeat)
    decor_shapes = [
        _to_decor(pkg, rels, ref, theme, decor_image_cache, decor_membership.get(i))
        for i, ref in enumerate(decor)
    ]

    return Pattern(
        pattern_id=f"{Path(slide_part).stem}",
        source_slide_index=[slide_number],
        layout_id=layout_id,
        kind=kind,
        slots=slots,
        repeat=repeat,
        decor=decor_shapes,
        capacity=capacity,
        score=score,
        is_dark=is_dark,
        kind_confidence=kind_confidence,
    )


def _slide_layout_part(pkg: PptxPackage, slide_part: str) -> str | None:
    related = pkg.related(slide_part, "slideLayout")
    return related[0] if related else None


def _resolve_slide_boxes(
    pkg: PptxPackage, refs: list[ShapeRef], layout_part: str | None, canvas: Canvas,
) -> list[ShapeRef]:
    """Плейсхолдер СЛАЙДА без собственного `a:xfrm` наследует позицию от
    плейсхолдера ЛЕЙАУТА того же типа — тот же принцип наследования, что и
    `layouts._resolve_placeholder_box` (лейаут → мастер), только на уровень
    ниже в цепочке (слайд → лейаут). Без этого шага заголовок почти каждого
    слайда — самый частый случай "позиция берётся из макета, слайд её не
    переопределяет" (бриф: "во всех 15 макетах ровно один плейсхолдер
    заголовка") — оставался бы `box=None` и вымывался бы на шаге `_visible`
    ещё до того, как мог бы стать headline. Резолвится по `ph_idx` (слайд
    ссылается на плейсхолдер лейаута именно по нему), а без совпадения
    `idx` — по `ph_type` как более мягкий фолбэк (та же асимметрия,
    что у `layouts._resolve_placeholder_box`)."""
    if layout_part is None:
        return refs
    unresolved = [r for r in refs if r.box is None and r.is_placeholder]
    if not unresolved:
        return refs

    layout_root = pkg.xml(layout_part)
    layout_refs = list(walk_shapes(layout_root, canvas, include_groups=False))
    by_idx = {r.ph_idx: r.box for r in layout_refs if r.is_placeholder and r.box is not None and r.ph_idx is not None}
    by_type: dict[str | None, Box] = {}
    for r in layout_refs:
        if r.is_placeholder and r.box is not None and r.ph_type not in by_type:
            by_type[r.ph_type] = r.box

    resolved = []
    for r in refs:
        if r.box is not None or not r.is_placeholder:
            resolved.append(r)
            continue
        new_box = by_idx.get(r.ph_idx) or by_type.get(r.ph_type)
        resolved.append(replace(r, box=new_box) if new_box is not None else r)
    return resolved


def _visible(ref: ShapeRef) -> bool:
    """Отбрасывает невидимое: нет координат (см. `ShapeRef.box`), нулевая
    площадь, шейп целиком за пределами холста (бриф, Step 2, п.1)."""
    if ref.box is None or ref.box.area <= 1e-6:
        return False
    return Box(0.0, 0.0, 1.0, 1.0).intersect(ref.box) is not None


def _picture_target(element, rels: dict[str, str]) -> str | None:
    blip_fill = element.find(qn("p:blipFill"))
    blip = blip_fill.find(qn("a:blip")) if blip_fill is not None else None
    rid = blip.get(qn("r:embed")) if blip is not None else None
    return rels.get(rid) if rid else None


def _shape_text(element) -> str:
    """Текст шейпа — `a:r/a:t` внутри каждого `a:p`, абзацы разделены `\\n`.

    Task 7 повторное ревью, находка №3: раньше функция собирала текст
    ТОЛЬКО из `p.findall(qn("a:r"))`, полностью игнорируя `a:br` (мягкий
    перенос строки) — у него и правда нет текстового узла, но пропустить
    его молча значит склеить текст ДО и ПОСЛЕ переноса без единого
    разделителя ("Заголовок" + `a:br` + "в две строчку" → "Заголовокв две
    строчку", встречалось 15/9/84/10 раз в четырёх шаблонах — системная
    порча, не краевой случай). Раз `a:br` в документном порядке стоит
    МЕЖДУ рядом идущими `a:r`, читать нужно ВСЕХ детей `a:p` по порядку, не
    только раздел `a:r` отдельно — иначе порядок и место разрыва потеряны.

    `a:br` → `\\n`, тот же разделитель, что и между абзацами (не пробел):
    мягкий перенос — это НОВАЯ строка внутри одного абзаца, семантически
    он ближе к границе абзаца, чем к пробелу между словами.

    Свойство теста `test_soft_line_break_does_not_corrupt_slot_text` — что
    в результате не появляется `\\x0b` — сохраняется по той же причине, что
    и раньше: `\\x0b` — это то, чем МЯГКИЙ ПЕРЕНОС отдаёт `python-pptx`
    через `.text_frame.text`, а этот модуль читает XML напрямую через lxml,
    такого слоя здесь просто нет."""
    tx_body = element.find(qn("p:txBody"))
    if tx_body is None:
        return ""
    lines = []
    for p in tx_body.findall(qn("a:p")):
        parts = []
        for child in p:
            tag = local_name(child)
            if tag == "r":
                t_el = child.find(qn("a:t"))
                parts.append(t_el.text or "" if t_el is not None else "")
            elif tag == "br":
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def _has_table(element) -> bool:
    return element.find(f".//{qn('a:tbl')}") is not None


def _has_chart(element) -> bool:
    graphic = element.find(qn("a:graphic"))
    graphic_data = graphic.find(qn("a:graphicData")) if graphic is not None else None
    uri = graphic_data.get("uri") if graphic_data is not None else ""
    return uri.endswith("/chart")


def _split_content_decor(
    refs: list[ShapeRef], rels: dict[str, str], logo_target: str | None, bg_targets: set[str],
) -> tuple[list[ShapeRef], list[ShapeRef]]:
    """Контентные шейпы (несут текст, картинку, таблицу) отдельно от декора
    (`noFill` без текста, линии, фоновые плашки, логотип — бриф, Step 2,
    п.2). Логотип/фоновая картинка узнаются по каталогу ассетов (`assets`,
    уже переданному вызывающим `mine_patterns`), не заново по тем же
    порогам — раз классификатор уже отличил их от контентных фото, нет
    смысла переизобретать это решение здесь."""
    content: list[ShapeRef] = []
    decor: list[ShapeRef] = []
    for ref in refs:
        if ref.kind == "connector":
            decor.append(ref)
        elif ref.kind == "picture":
            target = _picture_target(ref.element, rels)
            if target is not None and (target == logo_target or target in bg_targets):
                decor.append(ref)
            else:
                content.append(ref)
        elif ref.kind == "graphic_frame":
            content.append(ref)
        elif ref.kind == "shape":
            if ref.is_placeholder:
                # Task 7 повторное ревью, находка №1 (главная причина
                # обеднённого урожая на контрольном ЛЦТ2026): пустой
                # ПЛЕЙСХОЛДЕР — контентный слот с известной геометрией, а не
                # декор, даже когда в нём нет ни одного `a:r` — заполнителя
                # вписывает команда, которая будет пользоваться шаблоном
                # (слайды 8/9/10 ЛЦТ2026: `<p:ph type="title"/>` с полностью
                # разрешённой геометрией и без единого символа текста — то
                # же самое устройство, что у слайда 9, готовой карточной
                # раскладки на пять участников команды). Сам факт, что
                # дизайнер поставил здесь ИМЕННО плейсхолдер (не случайную
                # фигуру), — уже структурный сигнал "здесь должен быть
                # контент", вне зависимости от того, вписан ли в шаблон
                # текст-рыба. `sample_text` для такого слота — `None` (см.
                # `_tier_info`/`PatternSlot.sample_text` — поле уже
                # опционально ровно для этого случая).
                content.append(ref)
            elif _shape_text(ref.element).strip():
                content.append(ref)
            else:
                # НЕ-плейсхолдер без текста — вот это уже декор как он есть
                # (фигура без заливки и без текста, линия, фоновая плашка,
                # логотип): присутствие/отсутствие заливки само по себе не
                # меняет вывод — и подложка-плашка С заливкой, и голая
                # линия БЕЗ неё остаются декором, если это не плейсхолдер.
                decor.append(ref)
        # kind == "group" не встречается: walk_shapes(include_groups=False)
    return content, decor


# --- типографический разбор контентного шейпа ------------------------------


_BU_TAGS = (qn("a:buChar"), qn("a:buAutoNum"))


def _has_bullets(element) -> bool:
    tx_body = element.find(qn("p:txBody"))
    if tx_body is None:
        return False
    paragraphs = tx_body.findall(qn("a:p"))
    explicit = any(
        p.find(qn("a:pPr")) is not None and any(p.find(qn("a:pPr")).find(t) is not None for t in _BU_TAGS)
        for p in paragraphs
    )
    if explicit:
        return True
    # Без явной разметки буллета — вторичный сигнал: три и больше отдельных
    # непустых абзаца читаются как список пунктов, а не как один связный
    # абзац (округлая отсечка, не откалиброванная по трём файлам — три
    # строки короче типографской практики "абзац" и характернее для списка).
    non_empty = sum(1 for p in paragraphs if "".join(t.text or "" for t in p.iter(qn("a:t"))).strip())
    return non_empty >= 3


def _dominant_align(element, scale: TypeScale) -> str:
    tx_body = element.find(qn("p:txBody"))
    if tx_body is not None:
        for p in tx_body.findall(qn("a:p")):
            p_pr = p.find(qn("a:pPr"))
            algn = p_pr.get("algn") if p_pr is not None else None
            if algn:
                return algn
    return scale.default_align


def _dominant_color(element, theme: ThemeInfo) -> str | None:
    tx_body = element.find(qn("p:txBody"))
    if tx_body is None:
        return None
    for r in tx_body.iter(qn("a:r")):
        r_pr = r.find(qn("a:rPr"))
        if r_pr is None:
            continue
        solid = r_pr.find(qn("a:solidFill"))
        if solid is None:
            continue
        resolved = resolve_color(solid, theme.scheme, theme.clr_map)
        if isinstance(resolved, Color):
            return resolved.hex
    return None


def _shape_dominant_size(element, canvas: Canvas) -> float | None:
    """Мода нормированного кегля run'ов шейпа, взвешенная по числу символов
    — тот же принцип, что `_mode`/взвешивание в typography.py (общий приём
    задачи, здесь — свой маленький независимый расчёт, а не импорт приватной
    функции чужого модуля, см. докстроку `layouts.py::_placeholder_defrpr_sz`
    про этот же осознанный выбор)."""
    tx_body = element.find(qn("p:txBody"))
    if tx_body is None:
        return None
    votes: dict[float, int] = defaultdict(int)
    for r in tx_body.iter(qn("a:r")):
        r_pr = r.find(qn("a:rPr"))
        sz_raw = r_pr.get("sz") if r_pr is not None else None
        if sz_raw is None:
            continue
        t_el = r.find(qn("a:t"))
        chars = len(t_el.text or "") if t_el is not None else 0
        sz = round(int(sz_raw) / 100 * canvas.norm * 2) / 2
        votes[sz] += max(chars, 1)
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _nearest_step(scale: TypeScale, size_pt: float) -> str:
    return min(_TIER_ORDER, key=lambda k: abs(scale.steps.get(k, 0.0) - size_pt))


def _tier_info(ref: ShapeRef, canvas: Canvas, scale: TypeScale, theme: ThemeInfo) -> _TierInfo | None:
    if ref.kind != "shape":
        return None  # картинки/таблицы разбираются отдельно, см. _finalize_roles
    text = _shape_text(ref.element)
    stripped = text.strip()
    if not stripped and not ref.is_placeholder:
        # Пустой НЕ-плейсхолдер сюда попасть не должен вовсе (см.
        # `_split_content_decor` — он декор, в `content` не входит), но
        # защита остаётся: без текста и без структурного статуса
        # плейсхолдера измерять/классифицировать нечего.
        return None

    size_pt = _shape_dominant_size(ref.element, canvas)
    is_title_ph = ref.is_placeholder and ref.ph_type in TITLE_PH_TYPES
    if size_pt is None:
        size_pt = scale.steps.get("h1", 0.0) if is_title_ph else scale.steps.get("body", 0.0)

    step = _nearest_step(scale, size_pt)
    if is_title_ph and _TIER_RANK[step] < _TIER_RANK["h1"]:
        # Плейсхолдер заголовка почти всегда несёт роль headline независимо
        # от того, какой конкретно кегль у него на ЭТОМ слайде — не роняем
        # его в более младшую ступень только из-за нестандартного размера
        # шрифта на конкретном примере.
        step = "h1"

    numeric = bool(_NUMERIC_RE.match(stripped)) and len(stripped) <= 12
    bulleted = _has_bullets(ref.element)
    align = _dominant_align(ref.element, scale)
    color_hex = _dominant_color(ref.element, theme)
    return _TierInfo(step=step, size_pt=size_pt, numeric=numeric, bulleted=bulleted,
                      text=text if stripped else None, align=align, color_hex=color_hex)


# --- поиск повтора (бриф, Step 2, п.4) --------------------------------------


def _group_by_size(indices: list[int], boxes: list[Box]) -> list[list[int]]:
    """Группирует индексы по совпадающему размеру бокса (допуск
    `_SIZE_TOLERANCE`) — fixed-radius binning относительно ПЕРВОГО элемента
    группы, тот же приём, что `grid.cluster()` (см. её докстроку про отказ
    от расползания при сравнении с последней точкой)."""
    order = sorted(range(len(indices)), key=lambda i: (boxes[i].width, boxes[i].height))
    used = [False] * len(order)
    groups: list[list[int]] = []
    for oi in range(len(order)):
        i = order[oi]
        if used[oi]:
            continue
        group = [indices[i]]
        used[oi] = True
        base = boxes[i]
        for oj in range(oi + 1, len(order)):
            if used[oj]:
                continue
            j = order[oj]
            b = boxes[j]
            if abs(b.width - base.width) <= _SIZE_TOLERANCE and abs(b.height - base.height) <= _SIZE_TOLERANCE:
                group.append(indices[j])
                used[oj] = True
        groups.append(group)
    return groups


def _constant_step(coords: list[float]) -> float | None:
    """Расстояние между отсортированными координатами постоянно (в пределах
    `_STEP_TOLERANCE`) и достаточно велико, чтобы быть настоящим шагом
    повтора, а не шумом округления EMU одной и той же позиции (см. вызов
    ниже: `step > _STEP_TOLERANCE`, иначе "все почти в одной точке" ложно
    читалось бы как "шаг ~0")."""
    diffs = [b - a for a, b in zip(coords, coords[1:])]
    if not diffs:
        return None
    step = sum(diffs) / len(diffs)
    if step <= _STEP_TOLERANCE:
        return None
    if all(abs(d - step) <= _STEP_TOLERANCE for d in diffs):
        return step
    return None


def _spaced(boxes: list[Box]) -> tuple[str, float] | None:
    xs = sorted(b.left for b in boxes)
    ys = sorted(b.top for b in boxes)
    x_step = _constant_step(xs)
    y_step = _constant_step(ys)
    if x_step is not None and y_step is not None:
        return ("x", x_step) if x_step >= y_step else ("y", y_step)
    if x_step is not None:
        return "x", x_step
    if y_step is not None:
        return "y", y_step
    return None


@dataclass(frozen=True)
class _RepeatCandidate:
    axis: str
    count: int
    step: float
    indices: list[int]
    prelim_role: str


def _prelim_repeat_role(tier: _TierInfo | None, ref: ShapeRef) -> str:
    """Предварительная роль повторяемой группы — не финальная роль шейпа
    (та решается в `_finalize_roles` для ВСЕХ шейпов сразу), а признак,
    какого рода содержимое повторяется, нужный уже на этапе слияния
    кандидатов повтора в один `RepeatSpec` (см. `_find_repeat`)."""
    if ref.kind == "picture":
        square_ish = ref.box is not None and 0.8 <= (ref.box.width / max(ref.box.height, 1e-9)) <= 1.25
        return "icon" if square_ish and ref.box.width < 0.08 else "image"
    if tier is None:
        return "body"
    if tier.numeric:
        return "kpi_value"
    if _TIER_RANK[tier.step] >= _TIER_RANK["h2"]:
        return "card_title"
    if tier.bulleted:
        return "bullet"
    return "card_body"


def _aligned(a: "_RepeatCandidate", b: "_RepeatCandidate", content: list[ShapeRef]) -> bool:
    """Два кандидата повтора описывают ОДНУ И ТУ ЖЕ физическую сетку (не
    просто совпавшие по числу/шагу два независимых ряда где-то на слайде),
    если координаты их членов ВДОЛЬ ОСИ ПОВТОРА совпадают поэлементно
    (допуск `_SIZE_TOLERANCE`, тот же порядок величины, что и у совпадения
    размера бокса) — заголовок карточки №2 и тело карточки №2 стоят на ОДНОЙ
    и той же x-координате (при axis="x"), просто на разной высоте; два
    независимых ряда карточек с тем же шагом, но на разных x-стартах — уже
    другая сетка, сливать их значило бы придумать связь, которой на слайде
    нет (см. докстроку `_find_repeat` про находку №5 повторного код-ревью)."""
    coord = (lambda box: box.left) if a.axis == "x" else (lambda box: box.top)
    a_coords = sorted(coord(content[i].box) for i in a.indices)
    b_coords = sorted(coord(content[i].box) for i in b.indices)
    if len(a_coords) != len(b_coords):
        return False
    return all(abs(x - y) <= _SIZE_TOLERANCE for x, y in zip(a_coords, b_coords))


def _find_repeat(
    content: list[ShapeRef], tiers: list[_TierInfo | None],
) -> tuple[RepeatSpec | None, dict[int, str]]:
    """Ищет повторы контентных шейпов (бриф, Step 2, п.4): группирует по
    "роду" содержимого (текстовая ступень+буллет+числовой признак, либо
    "картинка"), внутри группы — по совпадающему размеру, и проверяет
    равный шаг вдоль одной оси. Кандидаты с совпавшими axis/count/step
    (разные "роды" одной и той же карточной сетки — заголовок карточки и
    тело карточки шагают синхронно) сливаются в ОДИН `RepeatSpec` — именно
    так пара title+body внутри карточки остаётся одной раскладкой, а не
    двумя независимыми повторами.

    Возвращает выбранный `RepeatSpec` (без `slot_roles` — их достраивает
    вызывающий уже по финальным ролям, см. `_mine_slide`) и словарь
    "индекс в `content` → предварительная роль" для шейпов, вошедших в
    повтор."""
    role_groups: dict[tuple, list[int]] = defaultdict(list)
    for i, (ref, tier) in enumerate(zip(content, tiers)):
        key = ("picture",) if ref.kind == "picture" else (tier.step, tier.numeric, tier.bulleted) if tier else None
        if key is None:
            continue
        role_groups[key].append(i)

    candidates: list[_RepeatCandidate] = []
    for key, idxs in role_groups.items():
        if len(idxs) < _MIN_REPEAT_COUNT:
            continue
        boxes = [content[i].box for i in idxs]
        for members in _group_by_size(idxs, boxes):
            if len(members) < _MIN_REPEAT_COUNT:
                continue
            member_boxes = sorted((content[i].box for i in members), key=lambda b: (b.left, b.top))
            spaced = _spaced(member_boxes)
            if spaced is None:
                continue
            axis, step = spaced
            prelim_role = _prelim_repeat_role(tiers[members[0]], content[members[0]])
            candidates.append(_RepeatCandidate(axis=axis, count=len(members), step=step,
                                                indices=members, prelim_role=prelim_role))

    if not candidates:
        return None, {}

    # Слияние кандидатов одной и той же физической сетки (см. докстроку).
    #
    # Task 7 повторное ревью, находка №5 (синтетический тест на саму суть
    # задачи): одного совпадения axis/count/step НЕДОСТАТОЧНО — оно слило
    # бы и два ПРОСТРАНСТВЕННО НЕСВЯЗАННЫХ ряда с одинаковым шагом и тем же
    # числом элементов (совпадение шага само по себе не редкость: 3-4
    # карточки с одним и тем же жёлобом сетки могут стоять где угодно на
    # слайде). Легитимное слияние (заголовок карточки + тело карточки одной
    # сетки) распознаётся не только по шагу, а по тому, что координаты
    # элементов ВДОЛЬ ОСИ ПОВТОРА у обеих групп совпадают поэлементно —
    # тот же ряд позиций (колонок сетки/строк), просто разного рода
    # содержимое, не просто "тот же шаг где-то ещё" (см. `_aligned`).
    used = [False] * len(candidates)
    merged_groups: list[list[_RepeatCandidate]] = []
    for i, c in enumerate(candidates):
        if used[i]:
            continue
        group = [c]
        used[i] = True
        for j in range(i + 1, len(candidates)):
            if used[j]:
                continue
            o = candidates[j]
            same_grid = (
                o.axis == c.axis and o.count == c.count and abs(o.step - c.step) <= _STEP_TOLERANCE
                and _aligned(c, o, content)
            )
            if same_grid:
                group.append(o)
                used[j] = True
        merged_groups.append(group)

    best = max(merged_groups, key=lambda g: (g[0].count, len(g)))
    axis, count, step = best[0].axis, best[0].count, best[0].step
    roles_by_index: dict[int, str] = {}
    for c in best:
        for idx in c.indices:
            roles_by_index[idx] = c.prelim_role

    # `len(best)` — число смёрженных кандидатов ОДНОЙ физической сетки, то
    # есть число слотов на одну повторяющуюся единицу (см. `RepeatSpec.
    # group_size`): каждый элемент `best` — самостоятельный кандидат
    # (заголовок карточки, тело карточки, ...), слитый в эту сетку именно
    # потому, что его члены выровнены с остальными вдоль оси повтора
    # (`_aligned`) — не то же самое, что число РАЗЛИЧНЫХ ролей
    # (`slot_roles`), которое может случайно схлопнуться, если несколько
    # элементов группы получили одну и ту же роль.
    return RepeatSpec(axis=axis, count=count, step=step, slot_roles=[], group_size=len(best)), roles_by_index


def _decor_repeat_membership(decor: list[ShapeRef], repeat: RepeatSpec | None) -> dict[int, int]:
    """Декор, принадлежащий ТОЙ ЖЕ повторяющейся сетке, что и `repeat`
    (уже найденный `_find_repeat` для текстовых слотов ЭТОГО ЖЕ слайда) —
    Task 9 повторное ревью, находка №1 ("главная находка"): шесть
    одинаковых карточных плашек под шестью карточками текста — это не
    шесть независимых украшений, а ОДИН декоративный элемент, повторённый
    шесть раз, и он часть той же группы, что и текстовые слоты.

    Принадлежность определяется ТЕМ ЖЕ МЕХАНИЗМОМ, что уже находит повтор
    текстовых слотов (`_find_repeat` выше): одинаковый размер декора
    (допуск `_SIZE_TOLERANCE`, `_group_by_size`), постоянный шаг вдоль оси
    (`_spaced`) — и этот шаг обязан совпасть (допуск `_STEP_TOLERANCE`) с
    ОСЬЮ и ШАГОМ уже найденного `repeat` контентных слотов: плашка карточки
    сидит ПОД карточкой с постоянным отступом, поэтому шаг между плашками
    (лево-в-лево/верх-в-верх) равен шагу между текстовыми слотами их
    карточек, даже если сами плашки крупнее слота (общий отступ сдвигает
    все плашки на одну и ту же величину, разница шага не меняется). Без
    привязки к `repeat.axis`/`repeat.step`, а только "нашли ряд одинаковых
    штук с постоянным шагом" — риск ложно принять за группу повтора
    декоративный орнамент/сетку линий, шагающую с СОВЕРШЕННО ДРУГИМ шагом,
    случайно совпавшим по количеству элементов; привязка к уже известному
    шагу content-повтора устраняет эту двусмысленность.

    Возвращает "индекс шейпа в `decor` -> позиция в группе" (0-based, по
    возрастанию координаты вдоль оси повтора) для ВСЕГО декора, вошедшего
    в такую сетку; пустой словарь, если у слайда нет повтора текстовых
    слотов вовсе, или ни одна группа декора не шагает тем же шагом по той
    же оси (декор остаётся вне группы — переносится статически, как и
    раньше).

    Task 10 отчёт, находка №3 ("плашки пропали", VK Tech "Риски
    раскатки"): у ОДНОЙ карточки почти всегда НЕСКОЛЬКО декоративных
    фигур разного размера — крупная плашка-подложка и мелкая иконка на
    ней, `_group_by_size` кластеризует их как РАЗНЫЕ группы (размер не
    совпадает), но ОБЕ шагают тем же шагом/осью, что и текстовый повтор
    (общий отступ карточки сдвигает и плашку, и иконку на одну и ту же
    величину). Раньше цикл возвращался на ПЕРВОЙ подошедшей группе
    (`return` внутри `for`) — опознавалась только она (обычно мелкая
    иконка, она чаще образует более чистый кластер по размеру), крупные
    плашки-подложки оставались `repeat_group=False` и переносились
    статически, независимо от фактического числа карточек: раскладка на
    четыре карточки под две карточки содержания рисовала ДВЕ иконки на
    своих местах, но ВСЕ ЧЕТЫРЕ исходные плашки — визуально "плашки
    карточек исчезли совсем, остались только синие квадратики-иконки".
    Теперь ВСЕ группы, подошедшие по оси/шагу, объединяются в одно
    членство — `expand_decor` разворачивает под фактическое число
    элементов и плашку, и иконку одной карточки вместе."""
    if repeat is None or repeat.step <= 0 or not decor:
        return {}
    indices = list(range(len(decor)))
    boxes = [ref.box for ref in decor]
    membership: dict[int, int] = {}
    for group in _group_by_size(indices, boxes):
        if len(group) < _MIN_REPEAT_COUNT:
            continue
        member_boxes = sorted((boxes[i] for i in group), key=lambda b: (b.left, b.top))
        spaced = _spaced(member_boxes)
        if spaced is None:
            continue
        axis, step = spaced
        if axis != repeat.axis or abs(step - repeat.step) > _STEP_TOLERANCE:
            continue
        coord = (lambda b: b.left) if axis == "x" else (lambda b: b.top)
        ordered = sorted(group, key=lambda i: coord(boxes[i]))
        membership.update({idx: pos for pos, idx in enumerate(ordered)})
    return membership


# --- назначение финальных ролей ---------------------------------------------


#  two_col — про ДВЕ колонки ТЕКСТА (body/bullet/card_body/subhead), не про
# любую пару контентных шейпов равной ширины: два изображения-сравнения
# рядом (VK Education, "Гистограмма и диаграмма с областями") геометрически
# проходят тот же тест "две равные по ширине штуки в одной строке", но по
# смыслу это не двухколоночный ТЕКСТ, а пара иллюстраций — не то, что
# должен означать kind="two_col" (см. отчёт задачи, регрессия на находку
# ручной проверки).
_TWO_COL_ROLES = frozenset({"body", "bullet", "card_body", "subhead"})

# "Минимальная ширина содержательной колонки" (было инлайн-литералом внутри
# старого `_column_pair`) — отсекает пары мелких значков, случайно
# оказавшихся рядом и одинаковых по ширине; округлая отсечка, не
# откалиброванная по трём файлам.
_MIN_COLUMN_WIDTH = 0.15

# Минимум "несколько" (бриф Task 7 повторного ревью №2, находка №1: карточку
# делает структура повтора — "несколько групп... и несколько элементов в
# каждой"). Одного слота на группу мало, чтобы называть повтор "карточками",
# двух уже достаточно (заголовок+тело или две строки внутри карточки).
_MIN_CARD_GROUP_SIZE = 2


def _column_groups(slots: list[PatternSlot]) -> list[list[int]]:
    """Группирует текстовые слоты (роли `_TWO_COL_ROLES`) в вертикальные
    полосы по горизонтальному положению — Task 7 повторное ревью №2,
    находка №2: колонка — это вертикальная ПОЛОСА, в которой лежит
    НЕСКОЛЬКО блоков (subhead и текст одной колонки слайда 11 контрольного
    ЛЦТ2026 слегка пересекаются по вертикали и совпадают по ширине — старый
    `_column_pair` читал их как два отдельных кандидата в колонку вместо
    одной полосы), а не отдельный текстовый блок сам по себе.

    Использует общую `grid.cluster()` (та же кластеризация, что для
    полей/колонн разметки в grid.py, тот же допуск `_SIZE_TOLERANCE`, что и
    раньше для сравнения ширины) на ГОРИЗОНТАЛЬНОМ ЦЕНТРЕ бокса
    (`left + width/2`), не на левом крае: подзаголовок и текст одной
    колонки реального слайда (11-й слайд контрольного ЛЦТ2026) не обязаны
    начинаться от одного и того же левого края день в день — ширина
    подзаголовка и абзаца внутри одной и той же колонки может отличаться на
    2%+ холста (уже больше `_SIZE_TOLERANCE`), при том что правый край у
    обоих совпадает почти точно. Левый край — произвольный выбор одной из
    двух сторон бокса, ничем не более обоснованный, чем правый; центр —
    единственная величина, не привязанная к тому, с какой стороны колонка
    "выровнена", и на практике устойчивее к обеим асимметриям сразу.

    `slots` сортируются по этому центру ДО вызова `cluster()`, чтобы
    `cluster()` получила уже отсортированный список — тогда её внутренний
    `sorted()` не меняет порядок (стабильная сортировка на уже
    отсортированных данных — тождество), и `cluster.count` элементов подряд
    из отсортированного `slots` можно взять как members кластера напрямую,
    без сопоставления по значению (которое было бы ненадёжным при
    повторяющихся координатах)."""
    boxable = [(i, s.box) for i, s in enumerate(slots) if s.role in _TWO_COL_ROLES]
    if len(boxable) < 2:
        return []
    boxable.sort(key=lambda t: t[1].left + t[1].width / 2)
    centers = [box.left + box.width / 2 for _, box in boxable]
    groups: list[list[int]] = []
    pos = 0
    for cl in cluster(centers, _SIZE_TOLERANCE):
        members = [boxable[pos + k][0] for k in range(cl.count)]
        pos += cl.count
        groups.append(members)
    return groups


def _columns(slots: list[PatternSlot]) -> list[list[int]]:
    """Полосы `_column_groups`, которые физически складываются в колонки:
    сопоставимая ширина (допуск `_SIZE_TOLERANCE`), не уже
    `_MIN_COLUMN_WIDTH`, не пересекаются по горизонтали. Возвращает ВСЕ
    подходящие полосы слева направо — две полосы дают "две колонки", три
    сопоставимые по ширине полосы дают "три колонки" (не режутся до двух),
    меньше двух подходящих полос — вовсе не колонки (пустой список).

    Ширина полосы для сравнения — СРЕДНЯЯ ширина её членов, не ширина
    объемлющего прямоугольника (`max(right) - min(left)` по всем членам):
    у объемлющего прямоугольника та же слабость, что была у старого
    `_column_pair` с левым краем — он берёт самый широкий левый выступ И
    самый широкий правый выступ, даже если это края РАЗНЫХ блоков полосы, и
    поэтому переоценивает ширину полосы ровно тогда, когда подзаголовок и
    абзац одной колонки чуть отличаются шириной друг от друга (11-й слайд
    контрольного ЛЦТ2026: подзаголовок правой колонки на ~2% холста шире
    тела). Средняя по членам полосы устойчива к этой асимметрии и не
    отличается от ширины объемлющего прямоугольника там, где все члены и
    так одной ширины (см. синтетику `test_columns_*`)."""
    bands = _column_groups(slots)
    measured = []
    for members in bands:
        boxes = [slots[i].box for i in members]
        left = min(b.left for b in boxes)
        right = max(b.right for b in boxes)
        content_width = sum(b.width for b in boxes) / len(boxes)
        if content_width > _MIN_COLUMN_WIDTH:
            measured.append((members, left, right, content_width))
    if len(measured) < 2:
        return []

    # Полосы, отличающиеся шириной от большинства (мелкий одинокий блок
    # рядом с настоящей колонкой), — не колонки: образец ширины — самая
    # частая среди подходящих по минимальной ширине полос, а не первая
    # попавшаяся.
    ref_width = max(
        measured, key=lambda m: sum(1 for o in measured if abs(o[3] - m[3]) <= _SIZE_TOLERANCE),
    )[3]
    matched = [m for m in measured if abs(m[3] - ref_width) <= _SIZE_TOLERANCE]
    matched.sort(key=lambda m: m[1])

    for prev, curr in zip(matched, matched[1:]):
        if curr[1] < prev[2] - _SIZE_TOLERANCE:
            return []  # полосы пересекаются по горизонтали — не настоящие колонки

    if len(matched) < 2:
        return []
    return [m[0] for m in matched]


def _finalize_roles(
    content: list[ShapeRef], tiers: list[_TierInfo | None], repeat_roles_by_index: dict[int, str],
    canvas: Canvas, scale: TypeScale,
) -> list[PatternSlot]:
    """Назначает роль КАЖДОМУ контентному шейпу — результат ровно того же
    размера и порядка, что `content` (индексы 1-в-1 переиспользуются ниже
    для capacity/kpi-пар/таблиц, см. `_mine_slide`/`_capacity`)."""
    n = len(content)
    roles: list[str | None] = [None] * n

    # 1. Повтор — предварительные роли уже решены на этапе поиска повтора.
    for i, role in repeat_roles_by_index.items():
        roles[i] = role

    # 2. Картинки/таблицы вне повтора.
    for i, ref in enumerate(content):
        if roles[i] is not None:
            continue
        if ref.kind == "graphic_frame":
            roles[i] = "table" if _has_table(ref.element) else ("chart" if _has_chart(ref.element) else "image")
        elif ref.kind == "picture":
            square_ish = ref.box is not None and 0.8 <= (ref.box.width / max(ref.box.height, 1e-9)) <= 1.25
            roles[i] = "icon" if square_ish and ref.box.width < 0.08 else "image"

    # 3. headline — самый верхний нерепитящийся текстовый шейп в
    # title-ступени (h1/display), либо явный плейсхолдер заголовка.
    title_candidates = [
        i for i, (ref, t) in enumerate(zip(content, tiers))
        if roles[i] is None and t is not None and _TIER_RANK[t.step] >= _TIER_RANK["h1"]
    ]
    headline_idx = None
    if title_candidates:
        placeholder_titles = [i for i in title_candidates if content[i].is_placeholder]
        pool = placeholder_titles or title_candidates
        headline_idx = min(pool, key=lambda i: content[i].box.top)
        roles[headline_idx] = "headline"

    # 4. KPI: числовой нерепитящийся слот -> kpi_value, ближайший
    # некрупный текст рядом -> kpi_label.
    kpi_indices = [
        i for i, t in enumerate(tiers)
        if roles[i] is None and t is not None and t.numeric
    ]
    for i in kpi_indices:
        roles[i] = "kpi_value"
    for i in kpi_indices:
        box = content[i].box
        candidates = [
            j for j, (ref, t) in enumerate(zip(content, tiers))
            if roles[j] is None and t is not None and _TIER_RANK[t.step] <= _TIER_RANK["caption"]
        ]
        near = [j for j in candidates if abs(content[j].box.top - box.bottom) < 0.05
                or abs(content[j].box.left - box.right) < 0.05]
        if near:
            roles[near[0]] = "kpi_label"

    # 5. Остальной текст — по ступени/буллетам/позиции.
    for i, (ref, t) in enumerate(zip(content, tiers)):
        if roles[i] is not None or t is None:
            continue
        near_bottom = ref.box.top >= 1 - _BOTTOM_BAND
        if _TIER_RANK[t.step] >= _TIER_RANK["h1"]:
            roles[i] = "subhead"
        elif t.step == "h2":
            roles[i] = "subhead"
        elif t.bulleted:
            roles[i] = "bullet"
        elif t.step in ("caption", "micro"):
            roles[i] = "source" if near_bottom else "caption"
        else:
            roles[i] = "body"

    # 6. Всё, что ещё без роли (нетекстовая графика без явной классификации
    # выше не встречается, но защищаемся честным дефолтом) -> body/image.
    for i, ref in enumerate(content):
        if roles[i] is not None:
            continue
        roles[i] = "image" if ref.kind == "picture" else "body"

    slots = []
    for ref, t, role in zip(content, tiers, roles):
        slots.append(_build_slot(ref, t, role, canvas, scale))
    return slots


_LINE_HEIGHT_EM = 1.25


def _build_slot(ref: ShapeRef, tier: _TierInfo | None, role: str, canvas: Canvas, scale: TypeScale) -> PatternSlot:
    if tier is not None:
        size_pt = tier.size_pt
        align = tier.align
        color_hex = tier.color_hex
        text = tier.text
    else:
        size_pt = 0.0
        align = scale.default_align
        color_hex = None
        text = None

    if role in _NON_TEXT_ROLES:
        max_chars = 0
        wraps = False
    else:
        max_chars = estimate_slot_chars(ref.box, canvas, size_pt)
        line_height_in = (size_pt / 72) * _LINE_HEIGHT_EM if size_pt else 0.0
        height_in = ref.box.height * canvas.height_in
        wraps = line_height_in > 0 and (height_in / line_height_in) >= 1.5

    return PatternSlot(
        role=role, box=ref.box, size_pt=round(size_pt, 1), color_hex=color_hex,
        align=align, max_chars=max_chars, wraps=wraps, sample_text=text,
    )


# --- вместимость слота: честная минимальная оценка (бриф, "Требования к
# работе") — заменить одной правкой на textfit из Task 9. -------------------

# Средняя ширина символа пропорционального гротескного шрифта относительно
# кегля (типографская характеристика семейства, не наблюдение за тремя
# файлами: 0.5-0.6 em/символ — общепринятый диапазон для гротесков) и
# межстрочный интервал в em (1.25 — типографская норма чуть шире
# одинарного). Обе величины — грубое приближение, честно объявленное
# заменяемым (см. докстроку `estimate_slot_chars`).
_AVG_CHAR_WIDTH_EM = 0.55
_MIN_SLOT_CHARS = 1


def estimate_slot_chars(box: Box, canvas: Canvas, size_pt: float) -> int:
    """Сколько знаков ПРИБЛИЗИТЕЛЬНО влезает в рамку `box` (доли холста)
    при кегле `size_pt` (уже в pt ЭТОГО холста, без дополнительной
    нормировки — `PatternSlot.size_pt` не нормирован к эталонному холсту,
    как `TypeScale.steps`, а взят как есть).

    НЕ замер глифов — площадная эвристика (символов в строке × строк в
    рамке, обе величины через типографские em-константы, см. их докстроки
    выше). Настоящий замер (учитывающий реальную ширину каждого глифа
    конкретного шрифта) — задача модуля `textfit`, Task 9 (бриф, "Требования
    к работе": "вынеси её в отдельную функцию, чтобы потом заменить на
    настоящий замер одной правкой"); эта функция — та самая точка замены,
    сигнатура (`box`, `canvas`, `size_pt` → число символов) рассчитана на
    то, чтобы остаться неизменной, когда внутри появится реальный замер.

    Минимум `_MIN_SLOT_CHARS` даже для вырожденной рамки — 0 неотличим от
    "не измерялось вовсе", а честная оценка обязана дать хоть какое-то
    число (тот же принцип, что и везде в задаче: явный, а не молчаливый
    отказ)."""
    width_in = box.width * canvas.width_in
    height_in = box.height * canvas.height_in
    if width_in <= 0 or height_in <= 0 or size_pt <= 0:
        return _MIN_SLOT_CHARS
    size_in = size_pt / 72
    char_width_in = size_in * _AVG_CHAR_WIDTH_EM
    line_height_in = size_in * _LINE_HEIGHT_EM
    chars_per_line = max(1, int(width_in / char_width_in))
    lines = max(1, int(height_in / line_height_in))
    return max(_MIN_SLOT_CHARS, chars_per_line * lines)


# --- классификация kind (бриф, Step 2, п.5, порядок приоритета дословно) ---


def _classify_kind(
    content: list[ShapeRef], slots: list[PatternSlot], repeat: RepeatSpec | None,
    roles_present: set[str], canvas: Canvas,
) -> tuple[str, float]:
    """Возвращает `(kind, confidence)` — `confidence` (задача "разбор
    незнакомого шаблона в бюджет", находка №2) размечает КАЖДУЮ ветвь этой
    функции отдельно, по тому, насколько структурный сигнал ветви сам по
    себе однозначен, а не подобрана под три учебных файла:

    - 1.0 (`cards`/`table`) — сигнал структурный и прямой: настоящий повтор
      ≥3 групп по ≥2 элемента, либо реальный `<a:tbl>` в содержимом. Спутать
      их геометрически почти не с чем — `config/pattern-kinds.yaml` не
      несёт вида, который выглядел бы так же.
    - 0.8 (`two_col`) — тоже структурный сигнал (выровненные по центру
      колонки текста), но мягче: основан на кластеризации с допуском
      (`_column_groups`/`_columns`), не на точном совпадении шага повтора.
    - 0.75 (`image`, весь слайд без единого текстового слота) — ролей
      кроме image/icon/chart нет вовсе, спутать с `photo_text` (который по
      определению словаря "фото И текст оба несут смысл") нечем — текста на
      слайде просто нет.
    - 0.45 (`kpi`, `image` с конкурирующим текстом) — реальный сигнал есть
      (числовой слот; картинка держит заметную площадь), но словарь видов
      прямо называет соседний вид, неотличимый ЭТИМ сигналом: `kpi_caption`
      (то же число, просто с развёрнутой подписью) и `photo_text` (та же
      картинка рядом с текстом, просто меньше формального порога площади).
    - 0.4 (`section`) — headline без единого содержательного тела: ИМЕННО
      этот случай назвала находка №1 брифа задачи 18 ("слайд с цитатой...
      геометрически неотличим от section") — самый частый источник
      схлопывания видов.
    - 0.3 (`bullets`) — буквально запасной вариант этой функции (последний
      `return`, когда ни одна из веток выше не подошла) — брифом задачи
      прямо назван как "запасной вариант «список»", наименее уверенный
      случай по построению, не по измерению.

    Порог, ниже которого паттерн стоит переспросить моделью, — забота
    `template.vision_kind` (0.5, см. её докстроку про ту же "половина —
    уже не увереннее монетки" логику, что и `profile._LOW_CONFIDENCE_
    THRESHOLD`), не этой функции: она размечает уверенность КАЖДОЙ ветви,
    а не решает, что с этой уверенностью делать дальше."""
    # Task 7 повторное ревью №2, находка №1: карточку делает СТРУКТУРА
    # повтора — несколько групп (>= 3, по горизонтали) с НЕСКОЛЬКИМИ
    # элементами в каждой (`group_size >= _MIN_CARD_GROUP_SIZE`), а не
    # присутствие заголовочного кегля внутри карточки. Раньше условие
    # требовало буквально card_title/kpi_value среди ролей повтора — верно
    # для трёх учебных файлов, где карточки случайно оказались с
    # заголовками, но ложно для слайда 9 контрольного ЛЦТ2026 (сетка из
    # пяти карточек участников команды, все три строки одного кегля, без
    # заголовочного кегля внутри карточки вовсе). Заголовочный кегль
    # (card_title/kpi_value как отдельная РОЛЬ на слайде) по-прежнему
    # повышает уверенность в результате — но уже не через это условие, а
    # тем, что он расширяет `roles_present` и напрямую поднимает
    # `role_score` в `_score` (см. её докстроку): типографика влияет на
    # SCORE раскладки, не на её KIND.
    if (
        repeat is not None and repeat.axis == "x" and repeat.count >= 3
        and repeat.group_size >= _MIN_CARD_GROUP_SIZE
    ):
        return "cards", 1.0

    if len(_columns(slots)) >= 2:
        return "two_col", 0.8

    if "kpi_value" in roles_present and repeat is None:
        return "kpi", 0.45

    if "headline" in roles_present and not ({"body", "bullet", "card_body", "card_title"} & roles_present):
        return "section", 0.4

    # Слайд целиком без текста (весь контент — картинка/иконка/график) —
    # доминирующая картинка/график по определению, независимо от площади:
    # текстовой иерархии здесь нет вообще, сравнивать площадь не с чем
    # (порог `_IMAGE_AREA_KIND_THRESHOLD` ниже имеет смысл только когда
    # картинка КОНКУРИРУЕТ с текстом за то, что "несёт смысл слайда" — на
    # контрольном ЛЦТ2026 одиночный график с полями/легендой занимает
    # ~30% площади, меньше порога, но текста рядом всё равно нет).
    if roles_present and roles_present <= {"image", "icon", "chart"}:
        return "image", 0.75

    for slot in slots:
        # "chart" здесь же, не отдельным kind'ом: интерфейс брифа (Step 2)
        # закрывает перечень kind'ов семью значениями (cards/two_col/kpi/
        # section/image/table/bullets), но по смыслу собственной оговорки
        # у "image" ("слайд, где смысл несёт картинка, а не текстовая
        # иерархия") доминирующий график — то же самое: не встретилось ни
        # на одном из трёх учебных файлов (бриф, "Что установила разведка",
        # п.4 — готовых диаграмм там нет вовсе), но встретилось на
        # контрольном ЛЦТ2026 (несколько слайдов "только график, без
        # заголовка"), и там это тот же случай, не отдельная категория.
        if slot.role in ("image", "chart") and slot.box.area > _IMAGE_AREA_KIND_THRESHOLD:
            return "image", 0.45

    if "table" in roles_present:
        return "table", 1.0

    return "bullets", 0.3


# --- проверки качества (бриф, "Требования к работе") ------------------------


def _slots_overlap(slots: list[PatternSlot]) -> bool:
    """Есть ли пара слотов, чьи боксы пересекаются заметной площадью —
    брифом: "слоты налезают друг на друга... в набор попадать не должна".
    Порог — доля площади МЕНЬШЕГО из двух боксов, не абсолютная площадь:
    два огромных слота, пересекающихся на волосок EMU-округления, не то же
    самое, что маленький слот, целиком проваленный внутрь другого."""
    for i in range(len(slots)):
        for j in range(i + 1, len(slots)):
            a, b = slots[i].box, slots[j].box
            inter = a.intersect(b)
            if inter is None:
                continue
            smaller = min(a.area, b.area)
            if smaller > 0 and inter.area / smaller > 0.3:
                return True
    return False


# Небольшое отклонение слота от измеренного поля, которое ещё считается
# шумом измерения, а не реальным выходом за поле, — `grid.margin_left`/
# `margin_right` сами по себе не архитектурная константа шаблона, а низкий
# ПРОЦЕНТИЛЬ распределения (см. докстроку `grid._MARGIN_PERCENTILE`): по
# самому определению процентиля какая-то часть реального контента шаблона
# систематически лежит чуть ДАЛЬШЕ него. Слот, отклонившийся не больше чем
# на это число, подравнивается к полю сдвигом БЕЗ изменения размера
# (`_snap_to_margins`) — честная компенсация статистической природы поля,
# а не искажение композиции; отклонение больше — уже настоящий выход за
# поле, такой паттерн отбраковывается `_within_margins` как есть. Округлая
# величина, не подобранная под три учебных файла: пять процентов холста —
# заметно меньше типичной ширины слота, но больше типичного зазора между
# процентильной оценкой поля и реальным систематическим кластером края
# (см. регрессии `grid.py` про VK Tech, где два разных кластера левого
# края лежат в 2-3 процентах друг от друга).
_MARGIN_SNAP_TOLERANCE = 0.05


def _snap_to_margins(slots: list[PatternSlot], grid: Grid) -> list[PatternSlot]:
    right_bound = 1 - grid.margin_right
    snapped = []
    for slot in slots:
        box = slot.box
        dx = 0.0
        left_gap = grid.margin_left - box.left
        right_gap = box.right - right_bound
        if 0 < left_gap <= _MARGIN_SNAP_TOLERANCE:
            dx = left_gap
        elif 0 < right_gap <= _MARGIN_SNAP_TOLERANCE:
            dx = -right_gap
        if dx:
            snapped.append(replace(slot, box=replace(box, left=box.left + dx)))
        else:
            snapped.append(slot)
    return snapped


def _within_margins(slots: list[PatternSlot], grid: Grid) -> bool:
    """Допуск чуть уже, чем у потребительской проверки "слоты в полях"
    (брифом — тест `test_slots_respect_template_margins`, допуск 0.01): раз
    та проверка — контракт для ВСЕХ намайненных паттернов, собственный
    фильтр обязан быть строже её, а не совпадать впритык — иначе плавающая
    погрешность (аффинные преобразования групп, округление EMU) может
    случайно протащить слот, который здесь прошёл, но там не пройдёт."""
    tol = 0.01
    for slot in slots:
        if slot.box.left < grid.margin_left - tol:
            return False
        if slot.box.right > 1 - grid.margin_right + tol:
            return False
    return True


def _score(slots: list[PatternSlot], repeat: RepeatSpec | None, roles_present: set[str]) -> float:
    """Пригодность паттерна к повторному использованию (бриф, Step 2, п.7):
    доля площади в полях, число ролей, отсутствие текста-рыбы, наличие
    repeat.

    "Доля площади в полях" ("Требования к работе" брифом) — это уже жёсткий
    ФИЛЬТР ДО вызова этой функции (`_within_margins`/`_slots_overlap` в
    `_mine_slide`: паттерн с слотом за полями или с наложением до `_score`
    просто не доходит), не отдельное слагаемое здесь: паттерн, прошедший
    эти проверки, УЖЕ на 100% "в полях" — взвешивать степень заполненности
    холста сверх этого было бы отдельным, ничем не обоснованным критерием
    "насыщенности" (плоская 4-секторная композиция с большим воздухом
    вокруг — тоже совершенно рабочая раскладка, не хуже плотной карточной
    сетки только потому, что занимает меньше площади холста).

    Оставшиеся два фактора брифа — число ролей (богаче структура —
    увереннее раскладка, `_ROLE_RICHNESS_CEILING` ролей и выше — уже
    "богатая" раскладка, дальше рост не даёт прироста) и repeat
    (параметричность — сама суть задачи, поэтому вес не меньше числа
    ролей) — оба весят поровну; отсутствие текста-рыбы — мягкий штраф
    (см. `_FISH_MARKERS`), не фильтр: почти весь текст на слайдах-примерах
    — рыба по построению задачи, полный запрет намайнил бы ноль паттернов."""
    if not slots:
        return 0.0
    role_score = min(1.0, len(roles_present) / _ROLE_RICHNESS_CEILING)
    # Repeat — не штраф его отсутствию (немало добротных паттернов вроде
    # "section"/"two_col"/"kpi" законно без повтора), а бонус его наличию:
    # 0.4 — честная "нейтральная" база, 1.0 — уверенно параметрический.
    repeat_score = 1.0 if repeat is not None else 0.4
    fish_hits = sum(
        1 for s in slots if s.sample_text and any(m in s.sample_text.lower() for m in _FISH_MARKERS)
    )
    fish_ratio = fish_hits / len(slots)

    score = 0.4 * role_score + 0.3 * repeat_score + 0.3 * (1 - fish_ratio)
    return max(0.0, min(1.0, score))


# --- вместимость на уровне всей раскладки ------------------------------


# Потолок числа пунктов, который раскладка вправе пообещать модели. ТЗ
# (Приложение 1, «Плотность») считает браком слайд, где больше шести
# буллетов, и `config/audit.yaml::density.max_bullets` держит ровно это
# число. Блок высотой в пол-слайда физически вмещает два десятка строк, но
# обещать их модели значит заказать слайд, который сами же и забракуем.
_MAX_BULLETS_PROMISED = 6


def _lines_that_fit(slot: PatternSlot, canvas: Canvas) -> int:
    """Сколько строк собственного кегля слота помещается в его высоту.

    `PatternSlot.size_pt` нормирован к эталонному холсту 13.333″ (см.
    `_shape_dominant_size`), а высота слота — доля РЕАЛЬНОГО холста, поэтому
    кегль денормируется: иначе на узком холсте (VK Tech, 10″) строк
    насчитывалось бы на треть меньше, чем влезает."""
    real_pt = slot.size_pt / canvas.norm if canvas.norm else slot.size_pt
    if real_pt <= 0:
        return 1
    line_height_in = (real_pt / 72) * _LINE_HEIGHT_EM
    height_in = slot.box.height * canvas.height_in
    return max(1, int(height_in / line_height_in))


def _capacity(
    content: list[ShapeRef], slots: list[PatternSlot], repeat: RepeatSpec | None, grid: Grid,
    canvas: Canvas,
) -> Capacity:
    body_like_slots = [s for s in slots if s.role in ("body", "card_body", "bullet")]
    body_like = [s.max_chars for s in body_like_slots]
    max_chars_per_item = max(body_like, default=0)

    # Сколько пунктов раскладка держит. Слоты с ролью "bullet" считаются
    # штучно — там каждый пункт лежит в своей рамке. Когда таких слотов нет,
    # пункты живут строками ВНУТРИ одного блока, и их число задаёт высота
    # блока, а не количество блоков.
    #
    # Раньше здесь стояла единица: «нет bullet-слотов — значит один пункт».
    # На живом прогоне 23 сентября 2026 (ЛЦТ2026, веб-интерфейс) это
    # обещало модели `max_bullets: 1` у раскладки, чей текстовый блок
    # занимает половину холста. Модель честно писала одну строку, код
    # рисовал её в блоке 5×6 дюймов — и аудит отмечал слайд как заполненный
    # на 2-14% холста. Пустота приходила не от модели и не от раскладки, а
    # отсюда: мы считали контейнеры вместо того, чтобы мерить место.
    bullet_count = sum(1 for s in slots if s.role == "bullet")
    if bullet_count:
        max_bullets = bullet_count
    elif body_like_slots:
        fits = max(_lines_that_fit(s, canvas) for s in body_like_slots)
        max_bullets = min(fits, _MAX_BULLETS_PROMISED)
    else:
        max_bullets = 0

    if repeat is not None and repeat.step > 0:
        span = (
            1 - grid.margin_left - grid.margin_right if repeat.axis == "x"
            else 1 - grid.margin_top - grid.margin_bottom
        )
        # Task 7 повторное ревью, находка №6 (мелочи): `span / step + 1`
        # считал, будто ПОСЛЕДНЯЯ карточка занимает только шаг, а не
        # собственную ширину/высоту сверх него — N карточек с шагом `step`
        # физически занимают `(N-1)*step + item_size`, не `N*step`, значит
        # правильная вместимость — `(span - item_size)/step + 1`, не
        # `span/step + 1` (переоценка на единицу при типичном step >
        # item_size, т.е. когда между карточками есть зазор). `item_size` —
        # размер УЖЕ намайненного слота той же роли, что и повтор (все члены
        # повтора прошли `_group_by_size` с допуском 2%, значит любой из них
        # представитель); 0.0 (честная деградация к старой формуле), если
        # почему-то ни один слот с ролью повтора не нашёлся — не должно
        # происходить, `repeat.slot_roles` строится из тех же самых слотов.
        item_size = next(
            (s.box.width if repeat.axis == "x" else s.box.height for s in slots if s.role in repeat.slot_roles),
            0.0,
        )
        usable_span = max(span - item_size, 0.0)
        # Вместимость по геометрии — сколько повторов такого шага реально
        # умещается между полями шаблона, не только то, что нарисовано на
        # исходном слайде-примере (тот и есть предмет параметризации).
        max_items = max(repeat.count, int(usable_span / repeat.step) + 1)
    else:
        max_items = 1

    max_rows = max_cols = 0
    for ref, slot in zip(content, slots):
        if slot.role != "table":
            continue
        tbl = ref.element.find(f".//{qn('a:tbl')}")
        if tbl is None:
            continue
        max_rows = max(max_rows, len(tbl.findall(qn("a:tr"))))
        grid_el = tbl.find(qn("a:tblGrid"))
        max_cols = max(max_cols, len(grid_el.findall(qn("a:gridCol"))) if grid_el is not None else 0)

    return Capacity(
        max_items=max_items, max_chars_per_item=max_chars_per_item, max_bullets=max_bullets,
        # Готовых графиков/диаграмм нет ни в одном из разведанных шаблонов
        # (бриф, "Что установила разведка", п.4) — max_series для kind
        # "chart" здесь неизмерим, честный 0, а не выдумка.
        max_series=0, max_rows=max_rows, max_cols=max_cols,
    )


# --- фон/тёмная тема слайда --------------------------------------------


def _pick_fill_element(container):
    for child in container:
        if local_name(child) in _BG_FILL_TAGS:
            return child
    return None


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _shape_fill_color(element, theme: ThemeInfo) -> Color | None:
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return None
    fill_el = _pick_fill_element(sp_pr)
    if fill_el is None or local_name(fill_el) in ("noFill", "grpFill", "gradFill", "blipFill", "pattFill"):
        return None
    resolved = resolve_color(fill_el, theme.scheme, theme.clr_map)
    return resolved if isinstance(resolved, Color) else None


def _decor_shape_luminance(decor: list[ShapeRef], theme: ThemeInfo) -> float | None:
    """Яркость самой крупной декоративной фигуры-подложки (площадь не
    меньше `_BG_AREA_SHARE`) со сплошной заливкой — `None`, если такой
    фигуры нет. Это была ЕДИНСТВЕННАЯ логика `_slide_is_dark` до Task 7
    повторного ревью (находка №4): работает, только когда фон СЛАЙДА
    нарисован декоративной плашкой-фигурой, а не задан `p:bg` (частый
    случай на трёх учебных шаблонах — см. докстроку `mine_patterns` про
    то, что фон почти всегда наследуется без собственного `p:bg`), и
    ничего не знает ни про картинку-фон, ни про градиент."""
    best: tuple[float, str] | None = None
    for ref in decor:
        if ref.kind != "shape" or ref.box is None or ref.box.area < _BG_AREA_SHARE:
            continue
        color = _shape_fill_color(ref.element, theme)
        if color is None:
            continue
        if best is None or ref.box.area > best[0]:
            best = (ref.box.area, color.hex)
    return _relative_luminance(best[1]) if best is not None else None


def _bg_chain_luminance(
    pkg: PptxPackage, theme: ThemeInfo, slide_part: str, layout_part: str | None,
    bg_image_cache: dict[str, float | None],
) -> float | None:
    """Яркость фона слайда по цепочке `p:bg` слайд → лейаут → мастер → lt1
    темы — ровно то, что уже умеет `layouts._resolve_background` (реюз её
    приватных хелперов, см. комментарий у импорта `_layouts` вверху файла):
    умеет градиент (цвет первой точки) и картинку-фон (пиксельный сэмпл
    средней яркости, с кэшем), не только сплошную заливку.

    Отличие от `layouts._resolve_background`: та функция резолвит фон
    ЛЕЙАУТА (готового каталога, без слайда в цепочке вовсе — у лейаута нет
    родительского слайда), а здесь нужен фон КОНКРЕТНОГО СЛАЙДА-ПРИМЕРА, у
    которого собственный `p:bg` (редко, но бывает и на слайде, не только
    на лейауте/мастере) стоит ВЫШЕ по цепочке наследования, чем у его
    лейаута — резолвим его сами тем же `_resolve_bg_color`/
    `_picture_luminance`, а на лейаут/мастер делегируем уже готовой
    `_resolve_background`, если у слайда своего `p:bg` нет. Сигнатура
    `mine_patterns` каталог макетов не принимает (бриф Task 7, Step 1,
    дословно) — читаем `p:bg` слайда и его макета напрямую тем же
    способом, что и `layouts.py`, а не через готовый `LayoutEntry`."""
    try:
        slide_bg = _layouts._bg_element(pkg.xml(slide_part))
        if slide_bg is not None:
            resolved = _layouts._resolve_bg_color(slide_bg, theme)
            if resolved.is_picture:
                return _layouts._picture_luminance(pkg, slide_part, resolved.picture_element, bg_image_cache)
            if isinstance(resolved.color, Color):
                return _relative_luminance(resolved.color.hex)
            return None
        if layout_part is None:
            return None
        layout_root = pkg.xml(layout_part)
        master_related = pkg.related(layout_part, "slideMaster")
        master_part = master_related[0] if master_related else None
        background = _layouts._resolve_background(pkg, layout_part, layout_root, master_part, theme, bg_image_cache)
        return background.luminance
    except Exception:
        # Битый цветовой модификатор в p:bg где-то по цепочке не должен
        # ронять майнинг всего слайда/шаблона из-за одного нерезолвящегося
        # фона (тот же принцип честной деградации, что у
        # `build_layout_catalog` вокруг `_resolve_background` — см. её
        # докстроку) — is_dark просто не резолвится этим путём, дальше
        # решает декоративная фигура/честный False-фолбэк в _slide_is_dark.
        return None


def _slide_is_dark(
    pkg: PptxPackage, theme: ThemeInfo, slide_part: str, layout_part: str | None,
    decor: list[ShapeRef], bg_image_cache: dict[str, float | None],
) -> bool:
    """Тёмная ли композиция слайда (Task 7 повторное ревью, находка №4).

    Приоритет источников, оба реальны на разведанных файлах:
    1. Декоративная фигура-подложка слайда (`_decor_shape_luminance`) —
       если она есть, это САМЫЙ прямой сигнал того, что реально видно на
       слайде: полноразмерная плашка часто нарисована ПОВЕРХ формально
       светлого `p:bg`, унаследованного от мастера (три учебных шаблона
       делают именно так — см. докстроку `mine_patterns`), и в этом случае
       именно она, а не декларация `p:bg`, определяет фактический цвет.
    2. Иначе — цепочка `p:bg` слайд→лейаут→мастер→lt1 (`_bg_chain_luminance`,
       реюз `layouts.py` — единственный источник, различающий картинку и
       градиент от плоского цвета; см. её докстроку). Нужна для нативного
       нестуденческого шаблона (ЛЦТ2026), где фон задан по-настоящему
       через `p:bg`, а не декоративной фигурой.
    3. Ни то ни другое не дало цвета — `False` (не "тёмный"), честный
       фолбэк, тот же принцип асимметричной безопасности, что и у
       `LayoutEntry.is_dark`."""
    luminance = _decor_shape_luminance(decor, theme)
    if luminance is None:
        luminance = _bg_chain_luminance(pkg, theme, slide_part, layout_part, bg_image_cache)
    if luminance is None:
        return False
    return luminance < _WCAG_DARK_THRESHOLD


_FILL_TAG_TO_KIND = {
    "noFill": "none", "solidFill": "solid", "gradFill": "gradient",
    "pattFill": "pattern", "blipFill": "picture", "grpFill": "group",
}


def _average_hex(colors: list[Color]) -> str | None:
    if not colors:
        return None
    r = sum(int(c.hex[1:3], 16) for c in colors) / len(colors)
    g = sum(int(c.hex[3:5], 16) for c in colors) / len(colors)
    b = sum(int(c.hex[5:7], 16) for c in colors) / len(colors)
    return f"#{round(r):02X}{round(g):02X}{round(b):02X}"


def _gradient_average_color(fill_el, theme: ThemeInfo) -> str | None:
    """Средний цвет градиента — среднее РАЗРЕШИВШИХСЯ цветов всех стоп-точек
    `a:gsLst/a:gs` (не только первой, как в `layouts._resolve_bg_color` —
    там одной точки достаточно для яркости "тёмный/светлый", здесь нужен
    представительный ЦВЕТ для рендера плашки, и усреднение по всем точкам
    честнее одной крайней)."""
    gs_lst = fill_el.find(qn("a:gsLst"))
    if gs_lst is None:
        return None
    colors = []
    for gs in gs_lst.findall(qn("a:gs")):
        color_el = next(iter(gs), None)
        if color_el is None:
            continue
        resolved = resolve_color(color_el, theme.scheme, theme.clr_map)
        if isinstance(resolved, Color):
            colors.append(resolved)
    return _average_hex(colors)


def _pattern_average_color(fill_el, theme: ThemeInfo) -> str | None:
    """Средний цвет узорной заливки — среднее переднего (`a:fgClr`) и
    заднего (`a:bgClr`) цветов узора: при типичной плотности штриховки оба
    вносят сопоставимый вклад в то, что видит глаз издалека, ни один не
    доминирует настолько, чтобы им одним заменить другой."""
    colors = []
    for tag in ("a:fgClr", "a:bgClr"):
        el = fill_el.find(qn(tag))
        color_el = next(iter(el), None) if el is not None else None
        if color_el is None:
            continue
        resolved = resolve_color(color_el, theme.scheme, theme.clr_map)
        if isinstance(resolved, Color):
            colors.append(resolved)
    return _average_hex(colors)


# Сторона уменьшенной копии картинки-заливки для оценки среднего цвета —
# тот же порядок величины и то же обоснование, что и
# `layouts._BG_IMAGE_SAMPLE_SIZE` (см. её докстроку): средний тон, не
# точный рендер, дёшево декодировать.
_DECOR_IMAGE_SAMPLE_SIZE = (16, 16)


def _picture_fill_average_color(
    pkg: PptxPackage, rels: dict[str, str], fill_el, cache: dict[str, str | None],
) -> str | None:
    """Средний цвет картиночной заливки декоративного шейпа — best-effort,
    тот же класс защит (`r:embed` не резолвится, часть отсутствует, формат
    не читается PIL), что и `layouts._picture_luminance`, только результат —
    хекс среднего цвета, а не яркость (для яркости годится готовая
    `layouts._picture_luminance`, но нужного здесь среднего ЦВЕТА (не
    яркости) у неё нет — маленький независимый хелпер, тот же приём, что
    everywhere в template/, см. докстроку `_layouts` про этот класс
    решений)."""
    blip = fill_el.find(qn("a:blip"))
    rid = blip.get(qn("r:embed")) if blip is not None else None
    if not rid:
        return None
    target = rels.get(rid)
    if not target:
        return None
    if target in cache:
        return cache[target]
    try:
        with Image.open(io.BytesIO(pkg.part(target))) as img:
            data = img.convert("RGB").resize(_DECOR_IMAGE_SAMPLE_SIZE).tobytes()
    except Exception:
        cache[target] = None
        return None
    if not data:
        cache[target] = None
        return None
    pixel_count = len(data) // 3
    r_total = sum(data[i] for i in range(0, pixel_count * 3, 3))
    g_total = sum(data[i] for i in range(1, pixel_count * 3, 3))
    b_total = sum(data[i] for i in range(2, pixel_count * 3, 3))
    hex_color = f"#{round(r_total / pixel_count):02X}{round(g_total / pixel_count):02X}{round(b_total / pixel_count):02X}"
    cache[target] = hex_color
    return hex_color


def _to_decor(
    pkg: PptxPackage, rels: dict[str, str], ref: ShapeRef, theme: ThemeInfo,
    decor_image_cache: dict[str, str | None], repeat_index: int | None = None,
) -> DecorShape:
    """`repeat_index` — позиция этого декора в группе повтора
    (`_decor_repeat_membership`), если он в неё вошёл; `None` (по
    умолчанию, обратная совместимость с прямыми вызовами из тестов) —
    декор вне группы повтора, `DecorShape.repeat_group` остаётся `False`."""
    sp_pr = ref.element.find(qn("p:spPr")) if ref.kind in ("shape", "connector") else None
    fill_el = _pick_fill_element(sp_pr) if sp_pr is not None else None
    fill_kind = _FILL_TAG_TO_KIND.get(local_name(fill_el), "unspecified") if fill_el is not None else "unspecified"
    has_fill = fill_kind not in ("none", "unspecified")

    fill_hex: str | None = None
    if fill_kind == "solid":
        color = _shape_fill_color(ref.element, theme)
        fill_hex = color.hex if color else None
    elif fill_kind == "gradient":
        fill_hex = _gradient_average_color(fill_el, theme)
    elif fill_kind == "pattern":
        fill_hex = _pattern_average_color(fill_el, theme)
    elif fill_kind == "picture":
        fill_hex = _picture_fill_average_color(pkg, rels, fill_el, decor_image_cache)

    prst, adj = _decor_prst_geom(ref.element) if ref.kind == "shape" else (None, None)

    return DecorShape(
        kind=ref.kind, box=ref.box, rotation=ref.rotation, flip_h=ref.flip_h, flip_v=ref.flip_v,
        fill_hex=fill_hex, has_fill=has_fill, fill_kind=fill_kind,
        repeat_group=repeat_index is not None, repeat_index=repeat_index or 0,
        prst=prst, adj=adj,
    )


def _decor_prst_geom(element) -> tuple[str | None, float | None]:
    """(`prst`, степень скругления) автофигуры-декора из `a:prstGeom` —
    независимая копия разбора `template/shapes.py::_prst_geom` (см.
    докстроку `DecorShape.prst`/`adj` про то, почему копия, а не импорт).
    `None, None`, если у шейпа нет `a:spPr`/`a:prstGeom`/`@prst` вовсе
    (например, `p:custGeom` — произвольный контур)."""
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return None, None
    geom = sp_pr.find(qn("a:prstGeom"))
    if geom is None:
        return None, None
    prst = geom.get("prst")
    if not prst:
        return None, None

    adj: float | None = None
    av_lst = geom.find(qn("a:avLst"))
    if av_lst is not None:
        for gd in av_lst.findall(qn("a:gd")):
            if gd.get("name") != "adj":
                continue
            fmla = (gd.get("fmla") or "").split()
            if len(fmla) == 2 and fmla[0] == "val":
                try:
                    adj = int(fmla[1]) / 100000
                except ValueError:
                    adj = None
            break
    return prst, adj


# --- дедупликация (бриф, Step 2, п.8) ---------------------------------------


def _dedup_signature(pattern: Pattern) -> tuple:
    slots_sig = tuple(sorted(
        (s.role, round(s.box.left, 2), round(s.box.top, 2), round(s.box.width, 2), round(s.box.height, 2))
        for s in pattern.slots
    ))
    return pattern.kind, slots_sig


def _dedup(patterns: list[Pattern]) -> list[Pattern]:
    """Паттерны с совпадающим `kind` и попарно близкими боксами слотов
    схлопываются, `source_slide_index` накапливается (бриф, Step 2, п.8)."""
    by_signature: dict[tuple, Pattern] = {}
    for p in patterns:
        sig = _dedup_signature(p)
        existing = by_signature.get(sig)
        if existing is None:
            by_signature[sig] = p
            continue
        merged_indices = sorted(set(existing.source_slide_index) | set(p.source_slide_index))
        winner = p if p.score > existing.score else existing
        by_signature[sig] = replace(winner, source_slide_index=merged_indices)
    return sorted(by_signature.values(), key=lambda p: -p.score)
