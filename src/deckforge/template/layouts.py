"""Каталог лейаутов шаблона и классификация их типа (`kind`).

В файле нет семантического типа лейаута: `p:sldLayout/@type` пуст у всех
84 лейаутов трёх учебных шаблонов (разведка, п.1) — классифицировать
приходится по имени и геометрии.

Ни один сигнал по отдельности не работает (разведка, п.2-3):
- **имя врёт** у WorkSpace — 11 из 15 лейаутов названы «Титульный слайд»
  Google-экспортом при сохранении, хотя структурно это обычные контентные
  раскладки (см. `test_workspace_meaningless_names_do_not_break_classification`);
- **геометрия однообразна** у VK Tech — 14 из 39 лейаутов несут только
  плейсхолдер TITLE и больше ничего, разница между «титульным» и
  «контентным» там видна не по числу/типу плейсхолдеров, а по их позиции
  и относительному кеглю заголовка.

Классификация — ансамбль имени (словарь синонимов `config/layout-kinds.yaml`,
вклад 0.6) и геометрической сигнатуры (вклад 0.4). Наивная сумма двух весов
по каждому кандидату независимо ЗДЕСЬ НЕ РАБОТАЕТ: у любого лейаута с
совпавшим именем комбинированный балл автоматически не ниже 0.6 (0.6*1 +
0.4*0), тогда как у конкурирующего типа без совпадения имени потолок — 0.4
(0.6*0 + 0.4*1) — имя побеждало бы ВСЕГДА, когда оно вообще совпало,
геометрия становилась бы бессмысленной ровно в том случае, который brief и
просит починить (WorkSpace). Вместо этого:

1. Геометрия голосует САМА ЗА СЕБЯ (`_geometry_scores` → argmax →
   `geometry_kind`), независимо от имени.
2. Имя голосует само за себя (`_name_scores` → argmax → `name_kind`; `None`,
   если имя не совпало ни с одной группой словаря).
3. Совпадают — берём этот тип, `kind_confidence = 0.6*name_score +
   0.4*geometry_score` (полное согласие, число близко к 1.0).
4. Не совпадают, но оба входят в «семью заголовочных» типов
   (`title`/`section`/`closing`, `_HEADING_FAMILY`) — геометрически они
   неотличимы (единственный крупный центрированный текстовый блок без
   иной структуры), поэтому расхождение внутри семьи — не сигнал ошибки,
   а такая же путаница, как в самом имени; тип берём по имени с тем же
   `kind_confidence`, что и в п.3.
5. Расходятся по-настоящему. Решает не "у кого балл выше" (это дало бы
   ложное срабатывание на кейсе "1_Свободный дизайн" у VK Tech — там имя
   говорит `free`, геометрия предпочитает `content` (0.75 против 0.6), но
   ОБА варианта геометрически правдоподобны сами по себе — расхождение
   между двумя похожими, оба разумными прочтениями не повод отбросить
   имя), а правдоподобен ли САМ КАНДИДАТ ИМЕНИ геометрически. Для kind'ов
   ВНЕ `_HEADING_FAMILY` (quote/kpi/...) правдоподобие — собственный
   геометрический балл (`geometry_scores[name_kind]`) не ниже
   `_GEOMETRY_DECISIVE_THRESHOLD`. Для title/section/closing — ПОВТОРНОЕ
   код-ревью (Task 5, п.2) заменило эту же проверку на относительную: имя
   правдоподобно, если этот лейаут — один из ЛИДЕРОВ по heading_score
   СРЕДИ ОСТАЛЬНЫХ ЛЕЙАУТОВ ЭТОГО ЖЕ ФАЙЛА (`_heading_leaders`, largest-gap
   разрыв в распределении баллов файла, не абсолютная константа — см. её
   докстроку и честную оговорку про калибровку `_GEOMETRY_DECISIVE_
   THRESHOLD` под три учебных файла, которую это заменяет). ЛИБО геометрия
   несёт прямое структурное свидетельство (`_HARD_EVIDENCE_KINDS`:
   PICTURE/TABLE-плейсхолдер физически есть в XML, колонки физически не
   пересекаются — не эвристика, а факт, и не применяется к самой
   `_HEADING_FAMILY` — см. докстроку `_HARD_EVIDENCE_KINDS`) — геометрии
   доверяем больше декларативного имени (случай WorkSpace: у "11_Титульный
   слайд" heading_score = 0.4279, НЕ входит в верхний уровень heading_score
   файла — три настоящих обложки WorkSpace на уровне ~0.735, разрыв 0.307,
   самый большой во всём файле), `kind_confidence = 0.4*geometry_score`
   победившего геометрией типа — заведомо ниже, чем при согласии (п.3-4),
   потому что имя здесь не подтвердило выбор.
6. Расходятся, но кандидат имени геометрически правдоподобен сам по себе
   (для НЕ-heading kind'ов — балл не ниже порога; для title/section/closing
   — лейаут лидирует по heading_score файла, и структурного факта против
   него нет) — доверяем имени, `kind_confidence = 0.6*name_score +
   0.4*geometry_component`, где `geometry_component` — собственный
   геометрический балл кандидата (НЕ-heading) либо margin отрыва от "фона"
   файла (heading-семья, см. `_heading_leaders` — брифом: "уверенность
   должна отражать величину отрыва").
7. Имя не совпало вовсе — решает одна геометрия, `kind_confidence =
   0.4*geometry_score`.

`_GEOMETRY_DECISIVE_THRESHOLD = 0.5` — используется в п.5-6 только для
kind'ов ВНЕ `_HEADING_FAMILY` (после повторного код-ревью, Task 5, п.2, см.
`_heading_leaders`): половина шкалы геометрического балла, кандидат имени
должен быть геометрически правдоподобен как минимум наравне со случайным
выбором. Не выведено из трёх файлов реверс-инжинирингом — округлая,
независимая от данных точка отсечения. Для title/section/closing ЭТОТ
абсолютный порог раньше калибровался циклом "тест на WorkSpace падает,
меняем веса `_heading_score`, тест проходит" — запас у настоящих обложек
WorkSpace был всего 0.07 от порога, а у "1_Свободный дизайн" VK Tech (для
`free`, тем же порогом) — тоже тонкий; честная оговорка про эту калибровку
и её замену на `_heading_leaders` — см. отчёт Task 5.
"""
from __future__ import annotations
import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from PIL import Image

from deckforge.ooxml.color import Color, UnresolvedColor, resolve_color
from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import local_name, qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import ShapeRef, walk_shapes
from deckforge.template.grid import Grid
from deckforge.template.theme import ThemeInfo, read_theme
from deckforge.template.typography import TypeScale, build_type_scale
from deckforge.template.usage import Usage, collect_usage

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "layout-kinds.yaml"

# Закрытый набор типов лейаута — контракт брифа (Step 1 интерфейса).
KINDS = (
    "title", "section", "content", "two_col", "three_col",
    "quote", "kpi", "image", "table", "closing", "free",
)

# Типы, геометрически неотличимые друг от друга (единственный крупный
# центрированный заголовочный блок без иной структуры) — см. докстроку
# модуля, п.4 алгоритма. Порядок внутри кортежа — приоритет тай-брейка
# ВНУТРИ семьи, когда имя вообще не дало сигнала (см. _classify): "section"
# первым — самый нейтральный смысл («заголовок раздела») из трёх, когда
# нечем подтвердить, что это именно обложка (title) или прощание (closing).
_HEADING_FAMILY = ("section", "title", "closing")

# "Мебель" слайда — плейсхолдер, присутствие которого почти не несёт
# структурного сигнала о типе лейаута (дата/номер/колонтитул встречаются
# наравне что на титульном, что на контентном лейауте) — исключается из
# структурных геометрических подсчётов (число содержательных плейсхолдеров,
# "одинокий доминирующий блок" для quote/kpi), но не из самого списка
# LayoutEntry.placeholders (это реальные плейсхолдеры лейаута, терять их
# из вывода нельзя).
_FURNITURE_PH_TYPES = frozenset({"FOOTER", "DATE", "SLIDE_NUMBER", "HEADER"})

_BODY_LIKE_PH_TYPES = frozenset({"BODY", "SUBTITLE", "OBJECT"})
_TITLE_PH_TYPES = frozenset({"TITLE", "CENTER_TITLE"})

# p:ph/@type (ECMA-376 ST_PlaceholderType) -> каноническое имя в верхнем
# регистре, как оно используется по всему тексту брифа ("TITLE", "BODY",
# "PICTURE", "FOOTER") и в python-pptx (PP_PLACEHOLDER) — не путать с сырым
# нижнерегистрым значением атрибута, которое отдаёт ShapeRef.ph_type
# (`walk.py` читает XML буквально). Отсутствие записи здесь не может
# произойти на валидном .pptx (ST_PlaceholderType — закрытый enum), но
# исключение из ST_PlaceholderType на будущее не роняет разбор: см. _canon_ph_type.
_PH_TYPE_CANON = {
    "title": "TITLE", "body": "BODY", "ctrTitle": "CENTER_TITLE", "subTitle": "SUBTITLE",
    "dt": "DATE", "sldNum": "SLIDE_NUMBER", "ftr": "FOOTER", "hdr": "HEADER", "obj": "OBJECT",
    "chart": "CHART", "tbl": "TABLE", "clipArt": "CLIP_ART", "dgm": "DIAGRAM",
    "media": "MEDIA_CLIP", "sldImg": "SLIDE_IMAGE", "pic": "PICTURE",
}

# WCAG 2.x относительная яркость: порог "тёмный фон" (брифом, Step 2 —
# буквально задан текстом, не подобран).
_DARK_LUMINANCE_THRESHOLD = 0.5

# "Верхняя четверть холста" — одна округлая, не подогнанная под конкретный
# файл константа, используемая дважды (см. докстройки _content_score и
# _heading_score): и как порог "плейсхолдер заголовка у самого верха —
# контентная конвенция", и как знаменатель линейной шкалы "чем ниже к
# центру, тем более "обложечно" расположен заголовок". Четверть высоты —
# типографская условность (область колонтитула/шапки в большинстве
# слайдовых и печатных сеток), не наблюдение за тремя учебными файлами.
_TOP_BAND = 0.25

# Допуск горизонтальной симметрии блока относительно оси 0.5 — половина
# "верхней четверти" не подходит по смыслу (разное измерение, ширина вместо
# высоты), поэтому отдельная округлая величина: 5% ширины холста — заметно
# уже типичной ширины плейсхолдера (20-90% в разведанных файлах), но
# достаточно, чтобы не отбраковывать центрирование из-за EMU-округления.
_SYMMETRY_TOLERANCE = 0.05

# Порог "предмет — не декоративная деталь, а содержательный блок" для
# geometry-сигнала quote/kpi (единственный крупный плейсхолдер без
# заголовка) — доля площади холста. 10% — заметно меньше типичной площади
# текстового блока (десятки процентов в разведанных файлах), но выше того,
# что может дать декоративная деталь/бейдж.
_MIN_DOMINANT_AREA_SHARE = 0.10

# Порог "геометрически правдоподобно" — половина шкалы геометрического
# балла, круглая точка отсечения (не выведена реверс-инжинирингом под три
# файла). Используется НЕ как "геометрия сама уверена в СВОЁМ кандидате", а
# как проверка кандидата ИМЕНИ: если у своего же (по имени) типа
# геометрический балл ниже порога — имя геометрически неправдоподобно,
# доверять ему нельзя (см. докстроку модуля, п.5, и _classify). Балл
# АЛЬТЕРНАТИВНОГО (по геометрии) кандидата в этом сравнении не участвует:
# у VK Tech "1_Свободный дизайн" geometry даёт content=0.75 против
# собственного (по имени) free=0.6 — оба выше порога, оба геометрически
# правдоподобны, поэтому имя (free) остаётся в силе, хотя у content балл
# выше.
#
# Повторное код-ревью, Task 5, п.2: для кандидатов ИЗ `_HEADING_FAMILY`
# (title/section/closing) эта проверка БОЛЬШЕ НЕ ПРИМЕНЯЕТСЯ — заменена на
# относительную (`_heading_leaders`, см. её докстроку и докстроку модуля):
# героический заголовок несопоставим по абсолютной величине между файлами
# (0.79 у ЛЦТ2026, 0.73 у WorkSpace, 0.43 у пограничных WorkSpace-случаев,
# 0.29 у "Разделителя" VK Tech), и у WorkSpace-случая (напр. "11_Титульный
# слайд", geometry title(имя)=0.4279) запас до 0.5 был всего 0.07 — эта
# константа калибровалась циклом "тест на WorkSpace падает, меняем веса
# _heading_score, тест проходит" (честная оговорка — см. отчёт Task 5).
# Для остальных kind'ов (quote/kpi/...) порог остаётся этим же, брифом
# находка касалась именно title-подобных типов.
_GEOMETRY_DECISIVE_THRESHOLD = 0.5

# "Жёсткие" геометрические признаки — не эвристический балл 0..1, а прямое
# структурное свидетельство (плейсхолдер PICTURE/TABLE физически есть в XML,
# колонки физически не пересекаются по горизонтали в одном ряду). Балл 1.0
# у этих kind'ов — не "похоже на", а "факт", и он обязан перевесить имя
# почти всегда, а не только когда собственный балл имени ниже порога (иначе
# лейаут с реальным плейсхолдером-картинкой, случайно названный в духе
# "раздела", навсегда остался бы "section" — имя для этих типов вообще не
# несёт структурного смысла, который стоило бы защищать). Исключение —
# `_HEADING_FAMILY` (см. _classify): обложка/разделитель/прощание законно
# могут нести декоративный плейсхолдер-картинку или пару текстовых колонок,
# не переставая быть обложкой/разделителем — эти признаки решают структуру
# КОНТЕНТНОГО лейаута, а не отменяют совсем другой по природе вопрос "это
# заголовочный слайд или нет" (найдено на контрольном ЛЦТ2026: макет
# "Титульный слайд" — ctrTitle + 2 декоративных PICTURE — без этого
# исключения перевешивался в "image", хотя это ровно первый слайд колоды).
_HARD_EVIDENCE_KINDS = frozenset({"image", "table", "two_col", "three_col"})

# Вклад сигналов в итоговую kind_confidence — брифом, Step 2, дословно.
_NAME_WEIGHT = 0.6
_GEOMETRY_WEIGHT = 0.4


@dataclass(frozen=True)
class Background:
    """Фон лейаута — цвет плюс откуда он взят и его относительная яркость.

    `source`: "layout" — свой `p:bg` лейаута (ЕСТЬ элемент `p:bg`, даже
    если его заливка — картинка/паттерн/нераспознанный цвет, не только
    плоский цвет); "master" — то же самое, но унаследовано от `p:bg`
    мастера (у лейаута своего элемента `p:bg` нет вовсе); "lt1_fallback" —
    ни там, ни там элемента `p:bg` НЕТ СОВСЕМ, подставлен `lt1` темы
    (брифом, Step 2: "фон резолвится по цепочке p:bg лейаута → p:bg
    мастера → lt1"); "resolve_error" — `p:bg` есть, но его разбор упал
    (битый цветовой модификатор и т.п. — не должно случаться на валидном
    .pptx, но не должно и ронять весь каталог, см. `build_layout_catalog`).
    Отличие "layout"/"master" от "lt1_fallback" содержательное: картинка-
    фон или нераспознанный цвет — это ЗАДАННЫЙ дизайнером фон, просто не
    сводящийся к одному цвету темы, а не "фон не задан" (adversarial-
    reviewer: раньше `blipFill` (фото-обложка) молча схлопывался в
    `lt1_fallback`, как будто фона нет вовсе, и реальный тёмный фон-фото на
    контрольном ЛЦТ2026 давал `is_dark=False`).
    `color` — `Color`/`UnresolvedColor`/`None` (три исхода, как и везде в
    ooxml.color — см. его докстроку): `None` — легитимно и для "фон есть,
    но это не один цвет" (картинка/паттерн), и для "даже lt1-фолбэк
    недоступен" (тема без цветовой схемы вовсе); различить эти два случая
    можно по `source`.
    `luminance` — относительная яркость WCAG 0..1. Для плоского цвета —
    точная формула по каналам; для картинки-фона (`source` — "layout"/
    "master", `color is None`) — среднее по уменьшенной копии изображения
    (см. `_picture_luminance`), `None`, если картинка не резолвится/не
    читается. Для любого другого случая, где цвет не резолвился
    (`UnresolvedColor`/`None` не от картинки), тоже `None` — так `is_dark`
    на LayoutEntry честно не гадает при неизвестном цвете (см. его
    докстроку).
    """
    color: Color | UnresolvedColor | None
    source: str
    luminance: float | None


@dataclass(frozen=True)
class PlaceholderSlot:
    """Один плейсхолдер лейаута. `box` — доли холста, гарантированно не
    `None`: плейсхолдер лейаута без собственного `a:xfrm` наследует позицию
    от плейсхолдера МАСТЕРА того же типа (см. `_resolve_placeholder_box`);
    если и там box не резолвится (на трёх учебных шаблонах такого не
    встретилось — см. отчёт про контрольный ЛЦТ2026), слот в список не
    попадает вовсе, а не протаскивается с `box=None` — контракт
    `test_placeholder_boxes_are_fractions` полагается на это буквально.
    `ph_type` — канонический тип из `_PH_TYPE_CANON` (см. её докстроку)."""
    ph_type: str
    box: Box


@dataclass(frozen=True)
class LayoutEntry:
    """Один макет каталога — контракт интерфейса брифа (Step 1) дословно.

    `layout_id` — имя файла части без расширения (`slideLayout11`), а не
    синтетический счётчик: уникально в пределах пакета, устойчиво к
    пересортировке `_layout_parts`.
    `master_index` — позиция мастера лейаута в `p:sldMasterIdLst`
    presentation.xml (см. `_master_order`), НЕ порядковый номер файла в
    архиве; `-1`, если мастер лейаута не резолвится вовсе или не входит в
    объявленный список мастеров презентации (на четырёх разведанных файлах
    не встретилось ни разу, но валидный .pptx формально это не запрещает).
    `kind`/`kind_confidence` — см. докстроку модуля и `_classify`.
    `is_dark` — `background.luminance is not None and < 0.5`; `False` (не
    "неизвестно"), если яркость не резолвилась — тот же принцип
    асимметричной безопасности, что и у `is_stock_office` в theme.py:
    неполные данные не должны ложно взводить сигнал "тёмный фон".
    `decor_count` — число НЕ-плейсхолдерных шейпов лейаута (`p:sp`/`p:pic`/
    `p:graphicFrame`/`p:cxnSp`, листья дерева, группы не считаются
    отдельно — `walk_shapes(..., include_groups=False)`), декоративная
    графика дизайна лейаута.
    `asset_refs` — имена частей `ppt/media/...`, на которые лейаут ссылается
    отношением типа `image` (отсортировано, без дублей) — включает и
    декоративные картинки, и картинку фона (`blipFill`), если она есть.
    `usage_count` — число слайдов шаблона, чей `p:sldLayout`-relationship
    указывает на этот лейаут.
    """
    layout_id: str
    part_name: str
    name: str
    master_index: int
    kind: str
    kind_confidence: float
    background: Background
    is_dark: bool
    placeholders: list[PlaceholderSlot]
    decor_count: int
    asset_refs: list[str] = field(default_factory=list)
    usage_count: int = 0


@dataclass(frozen=True)
class _RawLayout:
    """Всё посчитанное для ОДНОГО лейаута в первом проходе
    `build_layout_catalog`, кроме `kind`/`kind_confidence` — те решает
    второй проход, после того как посчитаны признаки ВСЕХ лейаутов файла
    (находка код-ревью Task 5, п.2: "титульность" лейаута по геометрии —
    его МЕСТО среди heading_score остальных лейаутов ЭТОГО ЖЕ файла, не
    абсолютная константа, поэтому классификация одного лейаута не может
    начаться раньше, чем посчитаны признаки всех остальных — см.
    `_heading_leaders`)."""
    layout_part: str
    name: str
    master_index: int
    background: Background
    is_dark: bool
    placeholders: list[PlaceholderSlot]
    decor_count: int
    asset_refs: list[str]
    features: _Features


def build_layout_catalog(
    pkg: PptxPackage, canvas: Canvas, theme: ThemeInfo, grid: Grid,
    *, usage: Usage | None = None, type_scale: TypeScale | None = None,
) -> list[LayoutEntry]:
    """Каталог лейаутов шаблона с классификацией `kind` (см. докстроку модуля).

    `theme` — тема ПЕРВИЧНОГО мастера (интерфейс брифа даёт ровно один
    `ThemeInfo`, не граф тем по частям, как в `usage.py`) — используется как
    честный фолбэк для лейаутов, чей собственный мастер по каким-то причинам
    не резолвится; для всех остальных резолв фона идёт по СОБСТВЕННОЙ теме
    мастера лейаута (`_theme_for_master`, кэшируется по мастеру за один
    проход) — на трёх учебных шаблонах с несколькими мастерами (VK Tech,
    Education) `dk1`/`lt1` совпадают у обоих мастеров, но полагаться на это
    для незнакомого шаблона с защиты (см. брифом, п.7: "число мастеров
    разное") значило бы рисковать неверным цветом там, где схемы правда
    расходятся, а не тихо давать верный ответ по случайному совпадению.

    `grid` в сигнатуре брифа присутствует, но геометрическая сигнатура этого
    модуля (позиция/кегль заголовка, площадь плейсхолдера, симметрия) не
    опирается на восстановленную сетку шаблона — сетка описывает КОНТЕНТ
    (слайды), а не архитектуру самих лейаутов, и колонные оси Grid.columns
    относятся к сетке в целом, не к конкретному лейауту. Параметр принят по
    контракту интерфейса и не отброшен молча.

    `usage`/`type_scale` — НЕОБЯЗЯТЕЛЬНЫЕ, уже посчитанные вызывающим
    `Usage`/`TypeScale` (находка код-ревью Task 5, п.4). Контракт: если
    переданы — используются КАК ЕСТЬ, без повторного вызова
    `collect_usage`/`build_type_scale` (это раньше делал сам
    `build_layout_catalog`, хотя вызывающий код часто уже вызвал их сам —
    см. `tests/template/conftest.py`; следующая задача, сборка профиля
    шаблона целиком, дёргает `collect_usage`/`build_type_scale` один раз на
    весь профиль и передаёт готовые сюда — без этого контракта двойной
    обход архива умножился бы). Если НЕ переданы (по умолчанию `None`) —
    вычисляются здесь же, тем же best-effort `try/except`, что и раньше —
    для обратной совместимости и для случая, когда `build_layout_catalog`
    вызывается сам по себе, без остального профиля. `type_scale.steps
    ["display"]` (кегль заголовка относительно него — один из геометрических
    признаков, брифом, Step 2) читается из готового `type_scale`, не
    пересчитывается отдельно.

    `collect_usage` (когда вызывается здесь) обходит ВЕСЬ пакет (все
    слайды/лейауты/мастера, не только тот лейаут, что каталогизируется
    прямо сейчас) — битый цвет ГДЕ УГОДНО в шаблоне не должен ронять каталог
    лейаутов целиком (тот же принцип, что и у `_resolve_background` для
    фона ОДНОГО лейаута — см. её докстроку; здесь блаcт-радиус шире, поэтому
    защита отдельная).
    """
    try:
        if usage is None:
            usage = collect_usage(pkg, canvas)
        if type_scale is None:
            type_scale = build_type_scale(pkg, canvas, usage)
        display = type_scale.steps.get("display") or 0.0
    except Exception:
        # display=0.0 — честное "неизвестно": _features трактует display=0
        # как "сравнивать не с чем" (короткое замыкание на falsy в
        # `title_size_ratio`), а не как "заголовок нулевого кегля".
        display = 0.0

    synonyms = _load_synonyms()
    master_order = _master_order(pkg)
    usage_count = _usage_count_by_layout(pkg)
    theme_cache: dict[str, ThemeInfo] = {}
    image_luminance_cache: dict[str, float | None] = {}

    # --- первый проход: геометрическая сигнатура и фон КАЖДОГО лейаута,
    # без классификации (см. докстроку _RawLayout). ---
    raw: list[_RawLayout] = []
    for layout_part in _layout_parts(pkg):
        layout_root = pkg.xml(layout_part)
        c_sld = layout_root.find(qn("p:cSld"))
        name = c_sld.get("name", "") if c_sld is not None else ""

        master_part = _master_of_layout(pkg, layout_part)
        master_idx = master_order.index(master_part) if master_part in master_order else -1
        layout_theme = _theme_for_master(pkg, master_part, theme, theme_cache) if master_part else theme

        try:
            background = _resolve_background(
                pkg, layout_part, layout_root, master_part, layout_theme, image_luminance_cache,
            )
        except Exception:
            # Битый p:bg (нечисловой/усечённый val цветового модификатора и
            # т.п.) не должен ронять каталог целиком ради одного лейаута —
            # см. докстроку _resolve_background и Background.source
            # ("resolve_error"). Тот же принцип, что уже применён рядом в
            # _theme_for_master для постороннего битого мастера.
            background = Background(color=None, source="resolve_error", luminance=None)
        is_dark = background.luminance is not None and background.luminance < _DARK_LUMINANCE_THRESHOLD

        refs = list(walk_shapes(layout_root, canvas, include_groups=False))
        placeholders = _placeholder_slots(pkg, refs, master_part, canvas)
        decor_count = sum(1 for r in refs if not r.is_placeholder)
        asset_refs = sorted(set(pkg.related(layout_part, "image")))
        master_title_size = _effective_master_title_size(pkg, master_part, layout_theme, canvas)
        features = _features(placeholders, display, refs, canvas, master_title_size)

        raw.append(_RawLayout(
            layout_part=layout_part, name=name, master_index=master_idx, background=background,
            is_dark=is_dark, placeholders=placeholders, decor_count=decor_count, asset_refs=asset_refs,
            features=features,
        ))

    # --- между проходами: место каждого лейаута среди heading_score ВСЕХ
    # лейаутов ЭТОГО файла (см. докстроку _heading_leaders). ---
    leaders = _heading_leaders([_heading_score(r.features) for r in raw])

    entries: list[LayoutEntry] = []
    for i, r in enumerate(raw):
        kind, kind_confidence = _classify(
            name=r.name, synonyms=synonyms, features=r.features,
            is_heading_leader=i in leaders, heading_margin=leaders.get(i, 0.0),
        )
        entries.append(LayoutEntry(
            layout_id=Path(r.layout_part).stem,
            part_name=r.layout_part,
            name=r.name,
            master_index=r.master_index,
            kind=kind,
            kind_confidence=kind_confidence,
            background=r.background,
            is_dark=r.is_dark,
            placeholders=r.placeholders,
            decor_count=r.decor_count,
            asset_refs=r.asset_refs,
            usage_count=usage_count.get(r.layout_part, 0),
        ))
    return entries


# --- словарь синонимов -----------------------------------------------------


@lru_cache(maxsize=1)
def _load_synonyms(path: str = str(_CONFIG_PATH)) -> dict[str, list[str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    kinds = data.get("kinds", {})
    return {kind: [p.lower() for p in patterns] for kind, patterns in kinds.items()}


_PREFIX_RE = re.compile(r"^\d+_")


def _name_scores(raw_name: str, synonyms: dict[str, list[str]]) -> dict[str, float]:
    """Голос имени за каждый kind — 0, если имя не совпало ни с одной
    группой; при совпадении с несколькими группами вес делится поровну
    между ними (см. докстроку config/layout-kinds.yaml про честную
    многозначность имени, а не ошибку словаря).

    Находка код-ревью Task 5, п.3: раньше совпадение было "любая подстрока
    любого паттерна побеждает" — более длинное и специфичное имя проигрывало
    более короткому и общему, если оба совпадали (стандартное имя PowerPoint
    "Two Content" содержит подстроку "content" и попадало в группу
    контентных, а не двухколоночных). Правило специфичности — по ТЕКСТУ, не
    по одной только длине: совпавший паттерн `p1` поглощается (не участвует
    в голосовании), если существует ДРУГОЙ совпавший паттерн `p2` из ДРУГОЙ
    группы, буквально содержащий `p1` целиком (`p1 in p2`) — тогда `p1` не
    несёт отдельного сигнала, это просто часть более специфичного текста.
    Паттерны, ни один из которых не substring другого (`"титул"`/`"раздел"`
    у "Титульный слайд раздела" — независимые, ни один не содержится в
    другом), остаются оба — это и есть честная многозначность, не то, что
    специфичность обязана схлопнуть."""
    stripped = _PREFIX_RE.sub("", raw_name).strip().lower()

    hits = [
        (kind, pattern)
        for kind, patterns in synonyms.items()
        for pattern in patterns
        if pattern in stripped
    ]
    if not hits:
        return {}

    survivors = [
        (kind, pattern) for kind, pattern in hits
        if not any(
            other_kind != kind and pattern != other_pattern and pattern in other_pattern
            for other_kind, other_pattern in hits
        )
    ]
    matched = sorted({kind for kind, _ in survivors})
    share = 1.0 / len(matched)
    return {kind: share for kind in matched}


# --- геометрическая сигнатура ----------------------------------------------


def _canon_ph_type(raw: str | None) -> str:
    if raw is None:
        return "BODY"  # walk.py: отсутствие p:ph значит "не плейсхолдер", сюда не попадает; страховка
    return _PH_TYPE_CANON.get(raw, raw.upper())


def _placeholder_defrpr_sz(element, canvas: Canvas) -> float | None:
    """Дублирует одноимённую функцию typography.py байт-в-байт — тот же
    сознательный выбор, что и у `_pick_fill_element`/`_bg_element` выше
    (см. их комментарий): маленький независимый хелпер, а не общий импорт
    поперёк модулей template/."""
    tx_body = element.find(qn("p:txBody"))
    lst_style = tx_body.find(qn("a:lstStyle")) if tx_body is not None else None
    lvl1 = lst_style.find(qn("a:lvl1pPr")) if lst_style is not None else None
    def_rpr = lvl1.find(qn("a:defRPr")) if lvl1 is not None else None
    sz_raw = def_rpr.get("sz") if def_rpr is not None else None
    return (int(sz_raw) / 100 * canvas.norm) if sz_raw is not None else None


def _row_groups(boxes: list[Box]) -> list[int]:
    """Группирует боксы по "строкам" (пересечение по вертикали) и внутри
    строки — непересечению по горизонтали (порядок слева направо) — сигнал
    двух/трёхколоночной раскладки (`two_col`/`three_col`). Возвращает
    размеры получившихся групп (в т.ч. одиночных, размер 1 — не колонка)."""
    ordered = sorted(boxes, key=lambda b: b.left)
    used = [False] * len(ordered)
    groups: list[list[int]] = []
    for i, box in enumerate(ordered):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        for j in range(i + 1, len(ordered)):
            if used[j]:
                continue
            other = ordered[j]
            same_row = not (other.top > box.bottom or box.top > other.bottom)
            last = ordered[group[-1]]
            # Небольшой допуск на стык колонок (плейсхолдеры дизайнера часто
            # примыкают впритык или с долей EMU нахлёста) — доля от того же
            # допуска симметрии, не отдельно откалиброванное число.
            non_overlapping = other.left >= last.right - _SYMMETRY_TOLERANCE / 5
            if same_row and non_overlapping:
                group.append(j)
                used[j] = True
        groups.append(group)
    return [len(g) for g in groups]


@dataclass(frozen=True)
class _Features:
    """Геометрическая сигнатура одного лейаута — сырьё для `_geometry_scores`."""
    n_content_placeholders: int
    has_title: bool
    has_picture: bool
    has_table: bool
    n_body: int
    largest_area_share: float
    title_top_center: float | None
    title_size_ratio: float | None
    symmetric_h: bool
    column_group_sizes: list[int]
    no_title_dominant: bool


def _features(
    placeholders: list[PlaceholderSlot], display: float, refs: list[ShapeRef], canvas: Canvas,
    master_title_size: float | None = None,
) -> _Features:
    content_ph = [p for p in placeholders if p.ph_type not in _FURNITURE_PH_TYPES]
    has_title = any(p.ph_type in _TITLE_PH_TYPES for p in placeholders)
    has_picture = any(p.ph_type == "PICTURE" for p in placeholders)
    has_table = any(p.ph_type == "TABLE" for p in placeholders)
    bodies = [p for p in placeholders if p.ph_type in _BODY_LIKE_PH_TYPES]

    # Находка код-ревью Task 5, п.1: раньше считался по ВСЕМ плейсхолдерам,
    # включая мебель (`_FURNITURE_PH_TYPES`) — докстрока этой константы уже
    # обещала исключать её из структурных геометрических подсчётов, но эта
    # конкретная величина была пропущена. На трёх учебных файлах эффекта не
    # было (мебель там крошечная, максимум 0.0418 при пороге 0.10), но на
    # нативном шаблоне с настоящим крупным колонтитулом это ложно взводило
    # "одинокий доминирующий блок" (quote/kpi, см. `_no_title_score`) по
    # площади подвала, а не контента. По `content_ph`, как и everywhere
    # ниже (`dominant`, `no_title_dominant`).
    largest_area_share = max((p.box.area for p in content_ph), default=0.0)

    title_slot = next((p for p in placeholders if p.ph_type in _TITLE_PH_TYPES), None)
    title_top_center = (title_slot.box.top + title_slot.box.height / 2) if title_slot else None

    title_ref = next(
        (r for r in refs if r.is_placeholder and _canon_ph_type(r.ph_type) in _TITLE_PH_TYPES), None,
    )
    title_sz = _placeholder_defrpr_sz(title_ref.element, canvas) if title_ref is not None else None
    if title_sz is None:
        # Находка код-ревью Task 5, п.6: на нативном шаблоне кегль
        # заголовка часто задан не в лейауте, а в `p:txStyles` мастера (20
        # из 23 макетов контрольного ЛЦТ2026) — без фолбэка title_size_ratio
        # вырождается в None почти всюду. `master_title_size` — уже
        # посчитанный вызывающим (`_effective_master_title_size`) кегль
        # мастера, честно `None`, если стили мастера деградировали (см. её
        # докстроку) — тогда здесь фолбэка нет, и None остаётся честным
        # ответом, а не выдумкой из заглушки Google-экспорта.
        title_sz = master_title_size
    title_size_ratio = (title_sz / display) if title_sz is not None and display else None

    dominant = title_slot or max(content_ph, key=lambda p: p.box.area, default=None)
    symmetric_h = False
    if dominant is not None:
        center_x = dominant.box.left + dominant.box.width / 2
        symmetric_h = abs(center_x - 0.5) <= _SYMMETRY_TOLERANCE

    column_group_sizes = _row_groups([p.box for p in bodies]) if len(bodies) >= 2 else []

    no_title_dominant = (
        not has_title
        and 1 <= len(content_ph) <= 3
        and largest_area_share >= _MIN_DOMINANT_AREA_SHARE
    )

    return _Features(
        n_content_placeholders=len(content_ph),
        has_title=has_title,
        has_picture=has_picture,
        has_table=has_table,
        n_body=len(bodies),
        largest_area_share=largest_area_share,
        title_top_center=title_top_center,
        title_size_ratio=title_size_ratio,
        symmetric_h=symmetric_h,
        column_group_sizes=column_group_sizes,
        no_title_dominant=no_title_dominant,
    )


def _heading_score(f: _Features) -> float:
    """Балл "единственный крупный центрированный заголовочный блок" — общий
    для всей `_HEADING_FAMILY` (title/section/closing, геометрически
    неразличимы, см. докстроку модуля). 0, если у лейаута нет плейсхолдера
    заголовка вовсе (quote/kpi/content и т.п. не должны получать бонус от
    этой ветки просто за малое число плейсхолдеров).

    Веса намеренно НЕ включают бонус за малое число плейсхолдеров (в
    отличие от более ранней версии) — разреженность структуры сама по себе
    не отличает обложку от контентного лейаута с заголовком-плейсхолдером и
    свободными фигурами вместо тела (брифом, п.5 — ровно WorkSpace): такой
    плоский бонус утягивал итоговый балл контентных лейаутов выше порога
    решительности геометрии только за счёт отсутствия прочих плейсхолдеров,
    маскируя слабость собственно "обложечных" признаков (позиции и кегля).
    Вес перенесён на позицию (0.6) и кегль относительно `display` (0.25) —
    именно они и есть настоящий геометрический смысл "это обложка/разделитель",
    не число плейсхолдеров.

    Повторное код-ревью (Task 5, п.2) потребовало обосновать САМИ веса
    смыслом признака, а не тем, что получилось на трёх файлах (итоговый
    балл ниже всё равно сравнивается не с абсолютной константой, а с
    heading_score остальных лейаутов файла, см. `_heading_leaders`, — но
    ВНУТРИ себя балл обязан складываться из содержательно ранжированных, а
    не подогнанных слагаемых):
    - **позиция заголовка (0.6, большинство)** — самый прямой геометрический
      смысл понятия "обложка/разделитель": заголовок, стоящий не у верхнего
      края, а ближе к центру/низу холста, — это буквально то, чем обложка
      ОТЛИЧАЕТСЯ от рабочего контентного слайда (там заголовок — якорь
      сверху, под ним идёт содержимое). Это не косвенный коррелят, а прямое
      определение признака, поэтому он несёт больше половины балла.
    - **кегль относительно `display` (0.25, второй по весу)** — крупный
      заголовок ТИПИЧЕН для обложки, но это опосредованный, менее надёжный
      сигнал: кегль отражает решение дизайнера о типографской иерархии
      вообще, а не конкретно о роли этого слайда, и физически отсутствует
      чаще, чем позиция (title_size_ratio — `None`, когда кегль не резолвится
      ни в лейауте, ни в мастере, см. `_effective_master_title_size`) —
      весомый, но подчинённый вклад.
    - **горизонтальная симметрия (0.15, наименьший)** — самый слабый и
      самый неоднозначный из трёх: центрирование заголовка — обычная
      дизайнерская привычка и на многих рабочих контентных слайдах с
      единственным заголовком-плейсхолдером, не только на обложках, поэтому
      само по себе почти ничего не говорит об "обложечности" — годится
      только как небольшое подтверждающее слагаемое, не как самостоятельный
      driver.
    Порядок значимости (позиция > кегль > симметрия) — содержательный и не
    зависит от чисел на конкретном файле; конкретные доли (0.6/0.25/0.15)
    выражают именно этот порядок и в сумме дают 1.0, чтобы шкала балла
    оставалась сопоставимой между файлами для `_heading_leaders`."""
    if not f.has_title:
        return 0.0
    score = 0.0
    if f.title_top_center is not None:
        # 0 у верхней четверти холста (контентная конвенция), 1.0 — от
        # вертикального центра и ниже (обложечная конвенция), линейно между.
        position = (f.title_top_center - _TOP_BAND) / _TOP_BAND
        score += 0.6 * max(0.0, min(1.0, position))
    if f.symmetric_h:
        score += 0.15
    if f.title_size_ratio is not None:
        score += 0.25 * min(1.0, f.title_size_ratio)
    return min(1.0, score)


def _content_score(f: _Features) -> float:
    score = 0.0
    if f.has_title and f.title_top_center is not None and f.title_top_center < _TOP_BAND:
        score += 0.5
    if f.n_body >= 1:
        score += 0.25
    elif f.n_content_placeholders <= 2:
        # Контент, вероятно, лежит в свободных фигурах слайда, а не в
        # плейсхолдерах лейаута (брифом, п.5 — WorkSpace).
        score += 0.15
    if not f.symmetric_h:
        score += 0.1
    return min(1.0, score)


def _free_score(f: _Features) -> float:
    if f.n_content_placeholders == 0:
        return 1.0
    if f.n_content_placeholders == 1:
        return 0.6
    return 0.0


def _no_title_score(f: _Features) -> float:
    if not f.no_title_dominant:
        return 0.0
    return 0.6 + (0.4 if f.symmetric_h else 0.0)


def _geometry_scores(f: _Features) -> dict[str, float]:
    heading = _heading_score(f)
    scores = {kind: 0.0 for kind in KINDS}
    for kind in _HEADING_FAMILY:
        scores[kind] = heading
    scores["content"] = _content_score(f)
    scores["free"] = _free_score(f)
    scores["image"] = 1.0 if f.has_picture else 0.0
    scores["table"] = 1.0 if f.has_table else 0.0
    no_title = _no_title_score(f)
    scores["quote"] = no_title
    scores["kpi"] = no_title
    # three_col побеждает two_col, когда у лейаута есть строки ОБОИХ видов
    # (напр. верхняя строка из 3 карточек и нижняя из 2 — контрольный
    # ЛЦТ2026, "Стадии": column_group_sizes=[3,3,2,2]) — не молчаливый
    # тай-брейк по порядку KINDS (нашёл adversarial-reviewer: раньше оба
    # получали score=1.0 одновременно, и argmax решал порядком словаря, а
    # не структурой), а содержательное правило: строка из 3 плейсхолдеров —
    # как минимум такая же по сложности раскладка, как строка из 2, поэтому
    # наличие тройки не должно теряться за счёт наличия где-то ещё и пары.
    scores["three_col"] = 1.0 if 3 in f.column_group_sizes else 0.0
    scores["two_col"] = 1.0 if (2 in f.column_group_sizes and 3 not in f.column_group_sizes) else 0.0
    return scores


def _heading_leaders(heading_scores: list[float]) -> dict[int, float]:
    """Кто из лейаутов ЭТОГО файла геометрически похож на обложку/раздел/
    прощание СИЛЬНЕЕ остальных — находка код-ревью Task 5, п.2 (см.
    докстроку модуля про калибровку `_heading_score`/абсолютного порога под
    известные файлы).

    Раньше "правдоподобие" кандидата имени из `_HEADING_FAMILY` сравнивалось
    с абсолютной константой (`_GEOMETRY_DECISIVE_THRESHOLD = 0.5`) — но
    heading_score НЕСОПОСТАВИМ по абсолютной величине между файлами (у
    контрольного ЛЦТ2026 настоящая обложка — 0.79, у WorkSpace — 0.73, у
    пограничных WorkSpace-случаев — 0.43, у VK Tech "Разделителя" — 0.29): у
    трёх настоящих титульных лейаутов WorkSpace запас до порога — 0.07, у
    "1_Свободный дизайн" VK Tech (для ДРУГОГО типа, тем же порогом) — тоже
    тонкий. Титульный/разделительный/прощальный лейаут — не "балл выше
    константы", а один из НЕМНОГИХ лейаутов ЭТОГО файла, чей heading_score
    заметно оторвался от heading_score остальных (брифом дословно).

    Метод — поиск САМОГО БОЛЬШОГО разрыва (`largest gap`) в отсортированном
    по убыванию списке РАЗЛИЧНЫХ значений heading_score этого файла: всё
    выше разрыва — лидеры, всё на разрыве и ниже — "фон" (типичный лейаут
    файла, чаще всего контентный, без героического заголовка). Разрыв,
    выбранный так, ищется не под известный ответ конкретного файла (нет ни
    одной константы, подобранной под число) — это то же самое, чем largest-
    gap/elbow-детекция является в общем случае: самая большая ступень в
    распределении значений статистически надёжнее отличает "сигнал" от
    "шума", чем любая заранее фиксированная точка отсечения, потому что она
    считается ЗАНОВО для КАЖДОГО файла из его собственных чисел.

    Проверено (см. отчёт Task 5) на всех трёх учебных файлах и на
    синтетике, не зависящей ни от одного из них
    (`test_heading_leaders_synthetic_set_independent_of_dataset_files`):
    - WorkSpace: разрыв между тремя настоящими обложками (~0.735) и двумя
      пограничными "Титульный слайд" (0.4279) — 0.307, самый большой во всём
      файле → лидируют только три настоящих (регрессия на брифом
      цитированный случай, `test_heading_leaders_matches_reverse_
      engineered_workspace_case`);
    - Education: разрыв между "Финальный с QR" (0.698, закрывающий, легитимно
      структурно похож на обложку — единственный крупный блок) и следующим
      уровнем (0.15) — 0.548, больше разрыва между ним и титульным (0.9205,
      0.222) → и титульные, И закрывающие лидируют, "раздела"-лейауты
      (0.1364, сливаются с фоном) — нет;
    - VK Tech: разрыв между самым слабым настоящим титульным/прощальным
      уровнем (0.5512) и "Разделителем" (0.2934) — 0.258, самый большой во
      всём файле → "Разделитель" остаётся ЗА пределами лидеров (тот же
      исход, что и раньше — см. отчёт про честную регрессию).

    `margin(i)` — насколько СОБСТВЕННЫЙ уровень значения heading_score[i]
    оторван от уровня, СРАЗУ следующего за найденным разрывом ("потолок
    фона") — не абсолютный балл, а именно отрыв, поэтому лейаут из самого
    верхнего уровня файла получает больший margin, чем лейаут из более
    слабого, но всё ещё лидирующего уровня (Education: title margin=0.77,
    closing margin=0.55) — брифом: "уверенность должна отражать величину
    отрыва". `margin` лежит в том же [0,1], что и сам heading_score (разность
    двух чисел из [0,1]), поэтому годится как компонента `kind_confidence`
    напрямую, без отдельной калиброванной шкалы.

    Возвращает индекс (позиция в ВХОДНОМ списке `heading_scores`, порядок
    `_layout_parts`) → margin для каждого лидера; индекс отсутствует в
    словаре — лейаут не лидер. Пустой словарь — весь файл на одном уровне
    (включая случай "героического заголовка нет вовсе", все нули) —
    сравнивать не с чем, лидировать не над чем.
    """
    distinct_desc = sorted(set(heading_scores), reverse=True)
    if len(distinct_desc) < 2 or distinct_desc[0] <= 0.0:
        return {}

    gaps = [distinct_desc[k] - distinct_desc[k + 1] for k in range(len(distinct_desc) - 1)]
    split_at = max(range(len(gaps)), key=lambda k: gaps[k])
    noise_ceiling = distinct_desc[split_at + 1]
    leader_values = set(distinct_desc[: split_at + 1])

    return {
        i: score - noise_ceiling
        for i, score in enumerate(heading_scores)
        if score in leader_values
    }


# --- ансамбль ----------------------------------------------------------

# Явный приоритет тай-брейка при точном равенстве баллов (обязан быть
# перестановкой KINDS целиком — проверяется тестом). НЕ через порядок
# ключей словаря/YAML: `max(dict, key=...)` при равенстве баллов
# возвращает первый по ПОРЯДКУ ВСТАВКИ ключ, а порядок вставки
# `geometry_scores`/`name_scores` — деталь реализации (порядок `KINDS`,
# порядок групп в `config/layout-kinds.yaml`), не документированный
# контракт (нашёл general-purpose ревьюер: докстрока модуля заявляла
# "section побеждает при равенстве внутри семьи title/section/closing" —
# нарочно поставив `_HEADING_FAMILY` с "section" первым, — но реально
# побеждал "title", потому что он раньше "section" в `KINDS`). Явный
# список делает тай-брейк тем, что он есть — осознанным решением, а не
# случайным следствием того, в каком порядке шли строки в YAML/кортеже:
# - "section" — раньше title/closing внутри `_HEADING_FAMILY` (см. её
#   докстроку: нейтральнее прочтение единственного центрированного
#   заголовочного блока без иного сигнала, чем "обложка" или "прощание");
# - "kpi" — раньше "quote" (`_no_title_score` намеренно не различает их —
#   доминирующий текстовый блок без заголовка одинаково похож на цитату и
#   на факт/цифру, геометрия здесь принципиально ничего не решает, только
#   имя; между двумя одинаково недоказанными вариантами порядок в списке —
#   произвольный, но ЗАЯВЛЕННЫЙ произвол, не случайность реализации).
_TIE_BREAK_PRIORITY = (
    "section", "kpi", "quote", "title", "closing",
    "content", "free", "two_col", "three_col", "image", "table",
)
assert set(_TIE_BREAK_PRIORITY) == set(KINDS)  # перестановка KINDS целиком


def _argmax(scores: dict[str, float]) -> str:
    """`max(scores)` с детерминированным тай-брейком по `_TIE_BREAK_PRIORITY`,
    а не по порядку ключей `scores` (см. её докстроку)."""
    best = max(scores.values())
    for kind in _TIE_BREAK_PRIORITY:
        if scores.get(kind, float("-inf")) == best:
            return kind
    return max(scores, key=lambda k: scores[k])  # недостижимо, если scores ⊆ KINDS


def _classify(
    *, name: str, synonyms: dict[str, list[str]], features: _Features,
    is_heading_leader: bool, heading_margin: float,
) -> tuple[str, float]:
    """См. докстроку модуля за полным описанием алгоритма (п.1-7).

    `features` — геометрическая сигнатура ЭТОГО лейаута, посчитанная
    вызывающим заранее (см. `_RawLayout`) — не пересчитывается здесь: она
    нужна была уже ДО классификации, чтобы посчитать heading_score всех
    лейаутов файла и определить `is_heading_leader`/`heading_margin` (см.
    `_heading_leaders`), пересчитывать её второй раз было бы лишней работой
    и риском разойтись с тем, что видел `_heading_leaders`.

    `is_heading_leader`/`heading_margin` — находка код-ревью Task 5, п.2:
    заменяют абсолютный порог `_GEOMETRY_DECISIVE_THRESHOLD` конкретно для
    кандидата имени из `_HEADING_FAMILY` (title/section/closing) — см.
    докстроку `_heading_leaders`. Для остальных kind'ов (quote/kpi/...)
    порог остаётся прежним, брифом эта находка касалась именно title-
    подобных типов, а не всей геометрической сигнатуры целиком.
    """
    geometry_scores = _geometry_scores(features)
    geometry_kind = _argmax(geometry_scores)
    geometry_score = geometry_scores[geometry_kind]

    name_scores = _name_scores(name, synonyms)
    name_kind = _argmax(name_scores) if name_scores else None
    name_score = name_scores.get(name_kind, 0.0) if name_kind else 0.0

    if name_kind is None:
        return geometry_kind, _GEOMETRY_WEIGHT * geometry_score

    if name_kind == geometry_kind:
        confidence = _NAME_WEIGHT * name_score + _GEOMETRY_WEIGHT * geometry_score
        return name_kind, confidence

    if name_kind in _HEADING_FAMILY and geometry_kind in _HEADING_FAMILY:
        # Геометрически неразличимы внутри семьи — не расхождение сигналов.
        confidence = _NAME_WEIGHT * name_score + _GEOMETRY_WEIGHT * geometry_scores[name_kind]
        return name_kind, confidence

    name_kind_geometry_score = geometry_scores.get(name_kind, 0.0)
    hard_evidence = (
        geometry_kind in _HARD_EVIDENCE_KINDS
        and geometry_score >= 1.0
        and name_kind not in _HEADING_FAMILY
    )
    if name_kind in _HEADING_FAMILY:
        # Находка код-ревью Task 5, п.2: "правдоподобие" кандидата имени из
        # title/section/closing больше не сравнивается с абсолютной
        # константой — решает МЕСТО heading_score этого лейаута среди
        # heading_score остальных лейаутов ЭТОГО ЖЕ файла (см. докстроку
        # `_heading_leaders`). `geometry_component` — тоже не абсолютный
        # heading_score, а margin отрыва: увереннее лейаут, оторвавшийся от
        # остальных сильнее (брифом дословно).
        name_implausible = not is_heading_leader
        geometry_component = heading_margin
    else:
        name_implausible = name_kind_geometry_score < _GEOMETRY_DECISIVE_THRESHOLD
        geometry_component = name_kind_geometry_score
    if hard_evidence or name_implausible:
        # Настоящее расхождение — и либо геометрия несёт прямое структурное
        # свидетельство (PICTURE/TABLE/непересекающиеся колонки — см.
        # _HARD_EVIDENCE_KINDS), либо кандидат ИМЕНИ геометрически
        # неправдоподобен сам по себе (для heading-семьи — не лидер среди
        # heading_score файла; для остальных kind'ов — балл ниже
        # _GEOMETRY_DECISIVE_THRESHOLD). Доверяем структуре, а не
        # декларативному имени (WorkSpace-случай — ровно ради него эта
        # ветка и введена).
        return geometry_kind, _GEOMETRY_WEIGHT * geometry_score

    # Геометрия предпочитает другой тип, но кандидат имени всё равно
    # геометрически правдоподобен — оба сигнала можно защитить, расхождение
    # — не повод отбросить имя (см. докстроку _GEOMETRY_DECISIVE_THRESHOLD,
    # кейс "1_Свободный дизайн", и докстроку `_heading_leaders` для
    # title/section/closing).
    confidence = _NAME_WEIGHT * name_score + _GEOMETRY_WEIGHT * geometry_component
    return name_kind, confidence


# --- фон и яркость -----------------------------------------------------

# `_pick_fill_element`/`_bg_element` дублируют одноимённые приватные хелперы
# usage.py байт-в-байт — сознательно, тем же приёмом, каким `_layout_parts`/
# `_slide_parts` уже независимо продублированы в grid.py/typography.py (и
# теперь в этом модуле, см. ниже): каждый модуль template/ читает свой
# небольшой срез XML сам, не тянет приватные функции другого модуля через
# границу файла — так модули остаются независимо читаемыми и не создают
# скрытую связность через нижнее подчёркивание "чужого" API.
_BG_FILL_TAGS = frozenset({"noFill", "solidFill", "gradFill", "grpFill", "pattFill", "blipFill"})


def _pick_fill_element(container):
    for child in container:
        if local_name(child) in _BG_FILL_TAGS:
            return child
    return None


def _bg_element(root):
    if root is None:
        return None
    c_sld = root.find(qn("p:cSld"))
    return c_sld.find(qn("p:bg")) if c_sld is not None else None


@dataclass(frozen=True)
class _ResolvedBg:
    """Результат `_resolve_bg_color` — три исхода, не два (см. докстроку
    `Background`): `color` — плоский цвет, когда заливка им является;
    `is_picture` — заливка есть, но это `blipFill` (картинка) — легитимная
    непрозрачная заливка БЕЗ единого цвета, не то же самое, что "заливки
    нет вовсе" (см. ниже, где раньше `blipFill` молча схлопывался в
    `None` наравне с отсутствием `p:bg` целиком — находка adversarial-
    reviewer: реальный тёмный фон-фото контрольного ЛЦТ2026 давал
    `is_dark=False`, потому что код падал в lt1-фолбэк, как будто фона нет
    вовсе)."""
    color: Color | UnresolvedColor | None
    is_picture: bool
    picture_element: object | None = None


def _resolve_bg_color(bg, theme: ThemeInfo) -> _ResolvedBg:
    """Цвет `p:bg` — заливка (`p:bgPr`) либо ссылка на тему (`p:bgRef`).
    Для градиента берётся цвет ПЕРВОЙ точки — этого достаточно для оценки
    яркости фона (задача не рендерит градиент, ей нужен один ориентировочный
    цвет, не точный визуал)."""
    bg_pr = bg.find(qn("p:bgPr"))
    if bg_pr is not None:
        fill_el = _pick_fill_element(bg_pr)
        if fill_el is None or local_name(fill_el) in ("noFill", "grpFill"):
            return _ResolvedBg(None, is_picture=False)
        if local_name(fill_el) == "blipFill":
            return _ResolvedBg(None, is_picture=True, picture_element=fill_el)
        if local_name(fill_el) == "gradFill":
            gs_lst = fill_el.find(qn("a:gsLst"))
            first_stop = next(iter(gs_lst), None) if gs_lst is not None else None
            color_el = next(iter(first_stop), None) if first_stop is not None else None
            color = resolve_color(color_el, theme.scheme, theme.clr_map) if color_el is not None else None
            return _ResolvedBg(color, is_picture=False)
        return _ResolvedBg(resolve_color(fill_el, theme.scheme, theme.clr_map), is_picture=False)

    bg_ref = bg.find(qn("p:bgRef"))
    if bg_ref is not None:
        color_el = next(iter(bg_ref), None)
        color = resolve_color(color_el, theme.scheme, theme.clr_map) if color_el is not None else None
        return _ResolvedBg(color, is_picture=False)
    return _ResolvedBg(None, is_picture=False)


def _relative_luminance_rgb(r: float, g: float, b: float) -> float:
    """Относительная яркость WCAG 2.x по каналам 0..1 — общее ядро для
    плоского цвета (`_relative_luminance`) и усреднённого пиксельного
    сэмпла картинки-фона (`_picture_luminance`)."""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _relative_luminance(hex_color: str) -> float:
    """Относительная яркость WCAG 2.x (без учёта альфы — см. докстроку
    Background: этому слою не дан задний фон, с которым композитить альфу)."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return _relative_luminance_rgb(r, g, b)


# Сторона уменьшенной копии картинки-фона для оценки средней яркости —
# 16×16 с запасом хватает для оценки среднего тона (не точного контраста
# по пикселю, только "фон в среднем тёмный или светлый"), но на порядок
# дешевле декодирования полноразмерного фото (в разведанных файлах — до
# нескольких МБ на изображение).
_BG_IMAGE_SAMPLE_SIZE = (16, 16)


def _picture_luminance(
    pkg: PptxPackage, bg_part: str, blip_fill_el, cache: dict[str, float | None],
) -> float | None:
    """Средняя относительная яркость картинки-фона (`a:blipFill`) —
    best-effort: не резолвится `r:embed`, нет такой части в архиве, битый/
    неподдерживаемый формат (emf/wmf и т.п., см. `PptxPackage._media_entry`
    про тот же класс проблем) — `None`, не падение (декоративное фото не
    должно ронять весь каталог лейаутов из-за одной нечитаемой картинки).

    `cache` — находка код-ревью Task 5, п.5: на контрольном ЛЦТ2026 22 из 23
    лейаутов ссылаются на ОДНУ И ТУ ЖЕ картинку-фон (3840×2160), и без кэша
    она декодировалась заново на каждый лейаут (почти вся полуторасекундная
    стоимость полного разбора — на этом). Ключ — резолвленное имя МЕДИА-
    парта (`target`), не `bg_part`/элемент: разные лейауты ссылаются на один
    и тот же медиа-файл через РАЗНЫЕ `r:embed`-идентификаторы (локальные для
    своих `.rels`), но резолвятся в одно и то же имя части. Кэш — параметр,
    не `lru_cache` на уровне модуля: живёт ровно один вызов
    `build_layout_catalog` (см. её докстроку), не переживает пакет, как и
    `theme_cache` рядом."""
    blip = blip_fill_el.find(qn("a:blip"))
    rid = blip.get(qn("r:embed")) if blip is not None else None
    if not rid:
        return None
    target = pkg.rels(bg_part).get(rid)
    if not target:
        return None
    if target in cache:
        return cache[target]

    try:
        with Image.open(io.BytesIO(pkg.part(target))) as img:
            # `Image.getdata()` уходит в Pillow 14 (DeprecationWarning на
            # прогоне тестов, брифом задачи — "почини заодно"). `tobytes()`
            # на RGB-изображении даёт тот же набор пикселей построчно, по 3
            # байта на пиксель — без промежуточного объекта `ImagingCore`,
            # который `getdata()` оборачивал.
            data = img.convert("RGB").resize(_BG_IMAGE_SAMPLE_SIZE).tobytes()
    except Exception:
        cache[target] = None
        return None
    if not data:
        cache[target] = None
        return None
    pixel_count = len(data) // 3
    total = sum(
        _relative_luminance_rgb(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255)
        for i in range(0, pixel_count * 3, 3)
    )
    luminance = total / pixel_count
    cache[target] = luminance
    return luminance


def _resolve_background(
    pkg: PptxPackage, layout_part: str, layout_root, master_part: str | None, theme: ThemeInfo,
    image_luminance_cache: dict[str, float | None],
) -> Background:
    """См. докстроку `Background` про три источника (`source`). Вызывающий
    (`build_layout_catalog`) оборачивает этот вызов в `try/except` — сама
    функция может дойти до `resolve_color` с произвольно битым `val`
    цветового модификатора (нечисловой `a:lumMod/@val`, усечённый
    `srgbClr/@val`) и бросить `ValueError` необработанным (adversarial-
    reviewer: раньше это ронял `build_layout_catalog` целиком из-за ОДНОГО
    испорченного цвета в ОДНОМ лейауте, хотя рядом, в `_theme_for_master`,
    для того же класса порчи постороннего мастера уже есть прецедент
    честной деградации — см. её докстроку).

    `image_luminance_cache` — сквозной кэш декодирования картинки-фона на
    весь каталог, см. докстроку `_picture_luminance`."""
    bg = _bg_element(layout_root)
    source = "layout"
    bg_part = layout_part
    if bg is None and master_part is not None:
        bg = _bg_element(pkg.xml(master_part))
        source = "master"
        bg_part = master_part

    if bg is None:
        # Ни лейаут, ни мастер `p:bg` НЕ ЗАДАЮТ ВООБЩЕ (элемента нет) —
        # фолбэк на lt1 темы (брифом, Step 2, дословно) — не "фона нет", а
        # "фон по умолчанию". Не путать со случаем ниже, где `p:bg` есть,
        # но не сводится к плоскому цвету (картинка/паттерн/нераспознанный
        # цвет) — там подстановка lt1 стёрла бы реально заданный (просто не
        # только-цветом) фон.
        lt1 = theme.scheme.get("lt1")
        color = Color(lt1) if lt1 else None
        luminance = _relative_luminance(color.hex) if isinstance(color, Color) else None
        return Background(color=color, source="lt1_fallback", luminance=luminance)

    resolved = _resolve_bg_color(bg, theme)
    if resolved.is_picture:
        luminance = _picture_luminance(pkg, bg_part, resolved.picture_element, image_luminance_cache)
        return Background(color=None, source=source, luminance=luminance)

    luminance = _relative_luminance(resolved.color.hex) if isinstance(resolved.color, Color) else None
    return Background(color=resolved.color, source=source, luminance=luminance)


# --- мастера, плейсхолдеры, использование -------------------------------


def _layout_parts(pkg: PptxPackage) -> list[str]:
    return sorted(n for n in pkg.names() if n.startswith("ppt/slideLayouts/slideLayout") and n.endswith(".xml"))


def _master_of_layout(pkg: PptxPackage, layout_part: str) -> str | None:
    related = pkg.related(layout_part, "slideMaster")
    return related[0] if related else None


def _master_order(pkg: PptxPackage) -> list[str]:
    """Порядок мастеров, как они объявлены в `p:sldMasterIdLst`
    presentation.xml — НЕ порядок файлов в архиве (`slideMaster1.xml`,
    `slideMaster2.xml`...): OOXML не гарантирует, что порядковый номер в
    имени файла совпадает с порядком объявления, а `master_index` брифом
    осмыслен именно как позиция в объявленном списке шаблона."""
    presentation_part = pkg.presentation_part()
    root = pkg.xml(presentation_part)
    lst = root.find(qn("p:sldMasterIdLst"))
    if lst is None:
        return []
    rels = pkg.rels(presentation_part)
    order = []
    for el in lst.findall(qn("p:sldMasterId")):
        rid = el.get(qn("r:id"))
        target = rels.get(rid)
        if target is not None:
            order.append(target)
    return order


def _theme_for_master(
    pkg: PptxPackage, master_part: str, fallback: ThemeInfo, cache: dict[str, ThemeInfo],
) -> ThemeInfo:
    """Тема КОНКРЕТНОГО мастера лейаута, не обязательно первичного — см.
    докстроку build_layout_catalog про то, почему резолв фона не может
    полагаться на единственный переданный `theme` для многомастерного
    шаблона. Ошибка чтения (посторонний мастер битый — тот же класс порчи,
    что и в usage.py::_ThemeGraph) не роняет каталог, фолбэк — тема,
    переданная вызывающим (обычно первичного мастера)."""
    if master_part in cache:
        return cache[master_part]
    try:
        resolved = read_theme(pkg, master_part)
    except Exception:
        resolved = fallback
    cache[master_part] = resolved
    return resolved


def _master_title_size(pkg: PptxPackage, master_part: str, canvas: Canvas) -> float | None:
    """Кегль заголовка из `p:txStyles/p:titleStyle/a:lvl1pPr/a:defRPr/@sz`
    мастера — тот же путь и та же нормировка (`canvas.norm`), что и
    `_placeholder_defrpr_sz` для лейаута, только на уровень выше в цепочке
    наследования OOXML (лейаут → мастер). Маленький независимый хелпер, не
    импорт приватной `theme.py::_style_level_triples` поперёк модулей —
    тот же сознательный выбор, что и у `_placeholder_defrpr_sz` (см. её
    докстроку)."""
    master_root = pkg.xml(master_part)
    tx_styles = master_root.find(qn("p:txStyles"))
    title_style = tx_styles.find(qn("p:titleStyle")) if tx_styles is not None else None
    lvl1 = title_style.find(qn("a:lvl1pPr")) if title_style is not None else None
    def_rpr = lvl1.find(qn("a:defRPr")) if lvl1 is not None else None
    sz_raw = def_rpr.get("sz") if def_rpr is not None else None
    return (int(sz_raw) / 100 * canvas.norm) if sz_raw is not None else None


def _effective_master_title_size(
    pkg: PptxPackage, master_part: str | None, theme: ThemeInfo, canvas: Canvas,
) -> float | None:
    """Кегль заголовка мастера, ГОТОВЫЙ к использованию как фолбэк для
    `_features` (находка код-ревью Task 5, п.6) — `None`, если мастер не
    резолвится ВООБЩЕ, либо его `p:txStyles` деградировал (заглушка
    Google-экспорта, `theme.text_styles_degraded` — см. её докстроку в
    theme.py): на трёх учебных шаблонах `txStyles` — одна и та же тройка на
    всех девяти уровнях, и подставлять оттуда кегль значило бы выдавать
    заглушку за измерение. На нативном шаблоне (не деградировавшем) это
    настоящий источник — см. отчёт Task 5."""
    if master_part is None or theme.text_styles_degraded:
        return None
    return _master_title_size(pkg, master_part, canvas)


def _resolve_placeholder_box(
    pkg: PptxPackage, ref: ShapeRef, master_part: str | None, canvas: Canvas,
) -> Box | None:
    """Box плейсхолдера лейаута: свой `a:xfrm`, а без него — наследование
    позиции от плейсхолдера МАСТЕРА того же типа (по `ph_type`, не по `idx`
    — на контрольном ЛЦТ2026 плейсхолдер `dt`/`ftr` без своего `a:xfrm`
    расходится с мастером по `idx` (у лейаута своя нумерация), но совпадает
    по типу с единственным плейсхолдером мастера того же типа — см. отчёт).
    `None`, если и там box не резолвится — такой плейсхолдер не попадает в
    LayoutEntry.placeholders вовсе (см. докстроку PlaceholderSlot)."""
    if ref.box is not None:
        return ref.box
    if master_part is None:
        return None
    master_root = pkg.xml(master_part)
    canon = _canon_ph_type(ref.ph_type)
    for master_ref in walk_shapes(master_root, canvas, include_groups=False):
        if master_ref.is_placeholder and _canon_ph_type(master_ref.ph_type) == canon and master_ref.box is not None:
            return master_ref.box
    return None


def _placeholder_slots(
    pkg: PptxPackage, refs: list[ShapeRef], master_part: str | None, canvas: Canvas,
) -> list[PlaceholderSlot]:
    slots = []
    for ref in refs:
        if not ref.is_placeholder:
            continue
        box = _resolve_placeholder_box(pkg, ref, master_part, canvas)
        if box is None:
            continue
        slots.append(PlaceholderSlot(ph_type=_canon_ph_type(ref.ph_type), box=box))
    return slots


def _usage_count_by_layout(pkg: PptxPackage) -> dict[str, int]:
    counts: dict[str, int] = {}
    for slide_part in sorted(n for n in pkg.names() if n.startswith("ppt/slides/slide") and n.endswith(".xml")):
        related = pkg.related(slide_part, "slideLayout")
        if not related:
            continue
        counts[related[0]] = counts.get(related[0], 0) + 1
    return counts
