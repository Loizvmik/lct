"""`TemplateProfile` — единый объект дизайн-системы, собранной из .pptx-шаблона.

Собирает воедино весь разбор предыдущих задач (тема, фактическая палитра и
шрифты, типографическая шкала, сетка, каталог лейаутов, каталог ассетов,
майнинг композиционных паттернов) плюс два требования этой задачи:

- именование ролей палитры моделью (`naming.py`) и, начиная с Task 18,
  уточнение вида раскладки мультимодальной моделью (`vision_kind.py`) — ДВА
  (не один, см. докстроку пакета — она обновлена этой задачей) модуля
  пакета, которые реально вызывают модель; `profile.py` сам импортирует из
  `deckforge.provider` только типы `LLMProvider`/`VisionProvider` для
  сигнатуры параметров `namer`/`vision` у `from_file` ниже, самого вызова
  здесь нет — вызов внутри `name_palette_roles_report`/`classify_patterns_
  by_vision`);
- человекочитаемый отчёт «откуда что взято» (`.provenance`) и список
  предупреждений о деградировавших источниках (`.warnings`), собранные из
  признаков происхождения и уверенностей, которые уже возвращает каждый
  модуль разбора (`ThemeInfo`, `Usage`, `TypeScale`, `Grid`, `AssetCatalog`).

`TemplateProfile` — pydantic-модель: все вложенные структуры сведены к
примитивам JSON (`str`/`float`/`int`/`bool`/`dict`/`list`), поэтому
`model_validate_json(profile.to_json()) == profile` держится без потерь —
дальше по профилю работает генератор, аудит и интерфейс, которым нужна
именно сериализуемая, а не питоновская объектная форма.
"""
from __future__ import annotations
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

from deckforge.ooxml.color import Color, UnresolvedColor
from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.provider.base import LLMProvider, VisionProvider
from deckforge.render.soffice import RenderError, to_pngs
from deckforge.settings import Settings
from deckforge.template.assets import AssetCatalog, AssetRef, Placement, build_asset_catalog
from deckforge.template.chart_palette import build_chart_series
from deckforge.template.grid import ColumnAxis, Grid, build_grid
from deckforge.template.layouts import Background, LayoutEntry, PlaceholderSlot, build_layout_catalog
from deckforge.template.naming import PaletteNote, name_palette_roles_report
from deckforge.template.patterns import (
    Capacity, DecorShape, Pattern, PatternSlot, RepeatSpec, chars_per_item, mine_patterns,
)
from deckforge.template.shapes import ShapeVocabEntry, build_shape_vocabulary
from deckforge.template.store import profile_key
from deckforge.template.theme import ThemeInfo, pick_primary_master, read_theme
from deckforge.template.typography import TypeScale, build_type_scale
from deckforge.template.usage import Usage, collect_usage
from deckforge.template.vision_kind import classify_patterns_by_vision, describe_pattern_slots

# config/app.yaml — единственная точка настройки, как и всё остальное в
# проекте (см. cli.py: тот же путь, тот же parents[N] от файла до корня
# репозитория — profile.py на один уровень глубже cli.py, отсюда [3], не [2]).
APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"

# Task 10: словарь автофигур шаблона — зеркало `ShapeVocabEntry`
# (template/shapes.py), нужен `compose/diagrams.py`, чтобы рисовать
# карточки схем формами, которые реально есть в шаблоне (см. докстроку
# shapes.py).
class ShapeVocabEntryModel(BaseModel):
    prst: str
    count: int
    avg_adj: float = 0.0


def _shape_vocab_entry_model(entry: ShapeVocabEntry) -> ShapeVocabEntryModel:
    return ShapeVocabEntryModel(prst=entry.prst, count=entry.count, avg_adj=entry.avg_adj)


# Версия СХЕМЫ `TemplateProfile` — Task 10 отчёт, находка №5: диск-кеш
# (`cache/profiles/<fingerprint>.json`) ключуется ТОЛЬКО отпечатком файла
# шаблона, без учёта версии кода/модели. Правка, добавляющая (или меняющая
# смысл) поле в `TemplateProfile` или во вложенную модель, не меняет
# отпечаток .pptx — старый файл кеша при этом молча продолжает
# подхватываться, а `pydantic` тихо подставляет дефолт для нового поля
# (или, того хуже, валидирует битую комбинацию как валидную), и профиль
# отдаётся БЕЗ новых данных, без единого предупреждения — именно это и
# увидел постановщик после правки, добавившей поля в модель. Бампать это
# число ОБЯЗАН каждый, кто меняет форму/смысл `TemplateProfile` или любой
# вложенной pydantic-модели (`LayoutEntryModel`, `PatternModel`,
# `AssetCatalogModel`, ...) — см. `from_file`, где версия сверяется ДО
# полной pydantic-валидации кеша, и разбор идёт заново при несовпадении
# (или отсутствии поля вовсе — кеш, записанный до появления этой версии).
# Task 10: версия поднята 2 -> 3 — добавлены поля `shape_vocabulary`,
# `chart_series`, `source_path` (см. докстроку выше про то, почему это
# ОБЯЗАТЕЛЬНО при любом изменении формы/смысла модели: без бампа старый
# диск-кеш подставил бы пустой список форм/цветов рядов молча, и
# `compose/diagrams.py`/`compose/charts.py` рисовали бы без словаря
# шаблона). 3 -> 4: смысл `chart_series` изменился ПОСЛЕ первого прогона
# смок-теста этой же задачи на этом же коде — живой замер на VK Tech
# отдавал `#C4C4C4`/`#FEFFFF` первыми двумя цветами (серый и почти-белый
# декоративный полутон, ни один не совпадает буквально ни с одной из
# hex-заливок нейтральных ролей палитры, поэтому фильтр по строковому
# совпадению их не ловил), см. `chart_palette._is_usable_series_color`
# (фильтр по насыщенности/светлоте). Без бампа локальный `cache/profiles/`
# этой же машины молча продолжал бы отдавать цвета ДО фикса.
# 4 -> 5: Task 10 код-ревью (после первого прогона задачи) — СМЫСЛ И
# `shape_vocabulary`, И `chart_series` поменялся снова, форма поля та же.
# `shape_vocabulary` теперь считается по декору карточных групп повтора
# намайненных раскладок, а не по переписи всех автофигур пакета (находка
# №1 — старый алгоритм давал один и тот же победивший прямой угол на всех
# шаблонах, различения не было, см. докстроку `template/shapes.py`).
# `chart_series` теперь дополнительно отсеивает кандидатов, неразличимых
# попарно (ΔE CIE Lab меньше порога), а не только по весу/насыщенности
# (находка №2 — `#FE095F`/`#FF0053` в одной палитре на контрольном
# шаблоне, см. докстроку `template/chart_palette.py`). Без бампа кеш
# продолжал бы молча отдавать словарь форм и палитру рядов, посчитанные
# СТАРЫМ (различения/различимости не проверявшим) алгоритмом.
# 5 -> 6: вторая правка код-ревью Task 10 (обязательный осмотр рендера
# нашёл дефект после первого прогона) — `shape_vocabulary` снова поменял
# СМЫСЛ: декор группы повтора засчитывается карточным, только если внутри
# него лежит текстовый слот раскладки, а не любой декор группы повтора
# (находка — круглые аватар-плашки/бейджи под иконку WorkSpace, без
# текста внутри, засчитывались наравне с настоящей карточной плашкой; см.
# докстроку `template/shapes.py`). Без бампа кеш молча продолжал бы
# отдавать словарь форм, посчитанный БЕЗ отбора по тексту.
# 6 -> 7: Task 11 повторное ревью аудита, находка №1 — `ColumnAxis.
# confidence` у направляющих (`p:guide`) больше не форсируется в 1.0
# независимо от реальной поддержки, добавлено поле `source` ("guide"/
# "cluster", см. докстроку `template/grid.py::ColumnAxis`). Без бампа кеш
# отдавал бы JSON старого формата, где у ВСЕХ осей (включая настоящие
# направляющие) `source` молча дефолтился бы в "cluster" — направляющая с
# крошечной поддержкой перестала бы быть безусловно достойной для L05,
# ровно то поведение, которое эта правка чинит.
# 7 -> 8: Task 18 — `PatternModel.kind` может теперь прийти не только из
# геометрии `patterns.py`, но и из уточнения мультимодальной моделью
# (`template/vision_kind.py::classify_patterns_by_vision`, вызывается из
# `from_file` ниже, когда передан `vision`). Форма поля та же (строка), но
# СМЫСЛ изменился — тот же случай, что уже описан у 3 -> 4/4 -> 5 выше
# ("смысл поля поменялся, форма нет, бамп всё равно обязателен"): кеш,
# записанный ДО этой правки, никогда не видел вид `quote`/`photo_text`/
# `kpi_caption` (`config/pattern-kinds.yaml`) — без бампа старый кеш молча
# продолжал бы отдавать только семь геометрических видов, даже когда
# сейчас передан ключ модели и разбор мог бы дать больше разнообразия.
# 8 -> 9: задача "разбор незнакомого шаблона в бюджет" — `PatternModel`
# несёт новое поле `kind_confidence` (зеркало `patterns.Pattern.
# kind_confidence`, см. её докстроку) — `template.vision_kind.classify_
# patterns_by_vision` теперь спрашивает модель ТОЛЬКО про паттерны с низкой
# уверенностью геометрии, а не про каждый паттерн подряд. Старый кеш (до
# этой правки) несёт паттерны БЕЗ этого поля — pydantic тихо подставил бы
# дефолт `1.0` ("уверен целиком"), и КАЖДЫЙ паттерн старого кеша читался бы
# как "геометрия уверена, модель звать не нужно" — молчаливая потеря самого
# смысла этой задачи (часть раскладок, которые раньше честно уточнялись
# моделью, перестала бы спрашиваться вовсе). Бамп версии заставляет такой
# кеш пересобраться заново, с настоящей уверенностью по ветвям `_classify_
# kind`, а не с фиктивной единицей.
# 9 -> 10: у `TemplateProfile` появилось поле `pattern_kinds_source` —
# признак «уточнялся ли вид раскладки моделью при сборке ЭТОГО профиля»
# (см. его комментарий у поля). Старый кеш этого поля не несёт вовсе, и
# pydantic подставил бы дефолт `"geometry"` — формально верно (все
# профили в `cache/profiles/` на момент правки собраны без `vision`), но
# ровно тот молчаливый случай, про который написана докстрока выше:
# смысл модели изменился, значит версия бампается, а не угадывается по
# дефолту. Заодно бамп чистит кеши, собранные ДО этой правки БЕЗ
# уточнения вида моделью, — их не придётся дочитывать на лету.
# 10 -> 11: изменился СМЫСЛ уже существующего поля `Capacity.max_bullets`
# (`template/patterns.py::_capacity`). Раньше раскладка без отдельных рамок
# под каждый пункт объявляла ровно один пункт; теперь считается, сколько
# строк влезает в высоту текстового блока (потолок — предел плотности ТЗ).
# Само поле старый кеш несёт, pydantic прочитал бы его молча и без ошибки —
# и вернул бы единицу, ту самую, из-за которой слайды выходили заполненными
# на 2-14% холста. Поле не появилось и не исчезло, поэтому поймать это
# нечем, кроме бампа версии: он заставляет кеш пересчитать вместимость
# заново.
# 11 -> 12: изменился РАЗБОР картинок слайда-примера (`patterns._split_
# content_and_decor`). Раньше контентом считалась каждая картинка, кроме
# логотипа и фона; теперь — только самая крупная, остальные уходят в декор
# и рисуются на слайде (`DecorShape.image_part`, `compose.decor._add_
# picture`). Поле `image_part` необязательное, старая запись прочиталась бы
# молча и без ошибки — и отдала бы раскладки БЕЗ фирменной графики, ради
# которой всё и делалось. Поймано живьём: пользователь загрузил шаблон
# заново, а получил прежний разбор из кеша.
# 12 -> 13: у `PatternSlot` появилось `anchor` — вертикальное выравнивание
# текста в рамке, снятое со слайда-примера. Старый кеш поля не несёт,
# pydantic подставил бы `"t"` (верх) — формально то же, что было, но тогда
# шаблон, где 40 слотов из 169 выровнены по центру (VK Education), так и
# остался бы с текстом, липнущим к верху высоких карточек.
# 13 -> 14: у `DecorShape` появились поля значка (`badge_text` и соседние).
# Старый кеш их не несёт, и нумерованные кружки шаблона снова стали бы
# пустыми местами под содержание.
# 15 -> 16: задача "превью PNG на каждый паттерн шаблона в кэше профиля" —
# `PatternModel` несёт новое поле `preview_path` (относительный путь к PNG
# первого исходного слайда паттерна рядом с JSON профиля, см. `_save_
# pattern_previews`). Формально поле НОВОЕ (не меняет смысл существующих),
# и pydantic тихо подставил бы дефолт `None` старому кешу без единой
# ошибки — но именно этот дефолт совпадает с "превью не сохранялось",
# поэтому кеш, записанный до этой правки, навсегда остался бы без превью,
# даже когда прогон, способный их сохранить (с ключом модели `vision`),
# случится позже и просто не будет знать, что кеш-хит нуждается в
# пересборке ради нового поля. Бамп версии заставляет такой кеш
# пересобраться заново, а не молча выдавать "превью нет" опытному прогону.
# 16 -> 17: `patterns._mine_slide` отбрасывает слайды-листы ассетов
# (больше `_MAX_DECOR_SHAPES` декоративных фигур). Набор паттернов в старом
# кеше на один лишний, и без бампа лист иконок так и оставался бы раскладкой.
# 17 -> 18: задача F — у `PatternSlotModel` появилась схема места от
# модели (`purpose`, `content_hint`, `max_words`, `ordinal`, `fixed`), у
# профиля — признак `pattern_schema_source`, а `Capacity.max_chars_per_item`
# теперь учитывает `max_words`. Старый кеш прочитался бы молча с пустой
# схемой и признаком «none», и дозапрос на кеш-хите это бы исправил, но
# версия бампается по правилу выше: смысл полей изменился.
# 18 -> 19: `patterns._mine_slide` отбрасывает слайды с примером кода
# (текст моноширинной гарнитурой): старый кеш VK Education держал такой
# слайд раскладкой two_col.
# 19 -> 20: задача I — у `PatternSlotModel` и `DecorShapeModel` появился
# `source_shape_id` (id исходной фигуры на слайде-примере, по нему клон
# находит фигуру без сравнения коробок), а `source_slide_index` ставит
# первым слайд, с которого сняты слоты (`patterns._dedup`). Старый кеш без
# id прочитался бы молча с `None`, и клон навсегда остался бы на угадывании
# по коробкам.
# 20 -> 21: подсказка «Вставить фото» в рамке становится слотом `image`
# (`patterns._promote_photo_placeholders`); старый кеш держал её подписью.
# 21 -> 22: задача J — схема места от модели несёт уверенность
# (`schema_confidence`), флаги `ordinal`/`fixed` принимаются только при
# уверенности не ниже 0,8, а `fixed` ещё и только для короткой фразы без
# подсказок дизайнера (`patterns.is_fixed_phrase`). В кеше v21 подсказка
# «Точки используются для навигации» могла лежать как `fixed`.
# 22 -> 23: задача R, у паттерна `source_density`, заполненность
# слайда-примера (D05 сравнивает клон с ней). Старый кеш прочитался бы с
# `None`, и D05 молча остался бы на глобальном коридоре.
PROFILE_SCHEMA_VERSION = 23

# Строка отчёта «откуда что взято» про вид раскладки: её пишет
# `_build_provenance` при полном разборе и она же ищется/заменяется при
# дозапросе видов моделью поверх кеш-хита (`_reclassify_pattern_kinds`) —
# одна константа на оба места, чтобы формулировка не разъехалась и строка
# не задвоилась в провенансе.
_NO_VISION_PROVENANCE_LINE = (
    "Вид раскладки (Pattern.kind) не уточнялся моделью — использован только "
    "геометрический майнинг (без ключа `vision`, семь корзин `patterns._classify_kind`)."
)

# Начала строк провенанса, которые говорят про вид раскладки: и сводка
# самой `classify_patterns_by_vision` ("Вид раскладки: N паттернов...",
# "Виды раскладки моделью не уточнялись: ..."), и строка выше.
_VISION_PROVENANCE_PREFIXES = ("Вид раскладки", "Виды раскладки")

# Ниже какой уверенности число из разбора попадает в предупреждения, а не
# только в тело отчёта. 0.5 — не наблюдение за тремя файлами, а сама природа
# доли/вероятности: ниже половины источник менее надёжен, чем монетка, и
# показывать такое число молча, без пометки, значит выдавать шаткую догадку
# за твёрдый факт.
_LOW_CONFIDENCE_THRESHOLD = 0.5

# Доля run'ов без явного стиля (Usage.unstyled_chars), после которой типовая
# шкала перестаёт быть надёжной сама по себе и стоит предупредить отдельно —
# не наблюдение за файлами (на трёх реальных шаблонах доля 8.5%-16.4%, это
# нормально и не требует отдельного предупреждения), а условие «четверть и
# больше текста без объяснимого стиля» — с такой долей уже сомнительно,
# отражает ли шкала типографику шаблона целиком.
_UNSTYLED_WARNING_SHARE = 0.25


# ---------------------------------------------------------------------------
# JSON-совместимые зеркала дата-классов разбора
# ---------------------------------------------------------------------------

class ColorValue(BaseModel):
    """Цвет либо нераспознанный цветовой элемент — зеркало `Color |
    UnresolvedColor` (см. ooxml/color.py) в форме, различимой без питоновских
    типов после JSON-круговорота."""

    resolved: bool
    hex: str | None = None
    alpha: float | None = None
    tag: str | None = None
    val: str | None = None
    reason: str | None = None


def _color_value(color: Color | UnresolvedColor | None) -> ColorValue | None:
    if color is None:
        return None
    if isinstance(color, Color):
        return ColorValue(resolved=True, hex=color.hex, alpha=color.alpha)
    return ColorValue(resolved=False, tag=color.tag, val=color.val, reason=color.reason)


class UnresolvedColorEntry(BaseModel):
    tag: str
    val: str | None
    reason: str


class ThemeModel(BaseModel):
    scheme: dict[str, str]
    clr_map: dict[str, str]
    major_font: str
    minor_font: str
    scheme_name: str
    font_scheme_degraded: bool
    text_styles_degraded: bool
    is_stock_office_palette: bool
    theme_font_share: float | None
    unresolved: list[UnresolvedColorEntry]


def _theme_model(theme: ThemeInfo) -> ThemeModel:
    return ThemeModel(
        scheme=dict(theme.scheme), clr_map=dict(theme.clr_map),
        major_font=theme.major_font, minor_font=theme.minor_font, scheme_name=theme.scheme_name,
        font_scheme_degraded=theme.font_scheme_degraded, text_styles_degraded=theme.text_styles_degraded,
        is_stock_office_palette=theme.is_stock_office_palette, theme_font_share=theme.theme_font_share,
        unresolved=[
            UnresolvedColorEntry(tag=u.tag, val=u.val, reason=u.reason) for u in theme.unresolved
        ],
    )


class TypeScaleModel(BaseModel):
    steps: dict[str, float]
    step_confidence: dict[str, float]
    heading_line_spacing: float
    heading_line_spacing_confidence: float
    body_line_spacing: float
    body_line_spacing_confidence: float
    default_align: str
    bold_is_idiomatic: bool
    italic_is_idiomatic: bool
    families: list[str]
    family_variants: dict[str, list[str]]
    families_total: int
    mono: list[str]


def _type_scale_model(scale: TypeScale) -> TypeScaleModel:
    return TypeScaleModel(
        steps=dict(scale.steps), step_confidence=dict(scale.step_confidence),
        heading_line_spacing=scale.heading_line_spacing,
        heading_line_spacing_confidence=scale.heading_line_spacing_confidence,
        body_line_spacing=scale.body_line_spacing,
        body_line_spacing_confidence=scale.body_line_spacing_confidence,
        default_align=scale.default_align, bold_is_idiomatic=scale.bold_is_idiomatic,
        italic_is_idiomatic=scale.italic_is_idiomatic, families=list(scale.families),
        family_variants={k: list(v) for k, v in scale.family_variants.items()},
        families_total=scale.families_total, mono=list(scale.mono),
    )


class ColumnAxisModel(BaseModel):
    center: float
    count: int
    confidence: float
    source: str = "cluster"


class GridModel(BaseModel):
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    columns: list[ColumnAxisModel]
    gutter: float
    anchors: dict[str, float]
    confidence: dict[str, float]
    skipped_no_box: int
    native_guides_used: bool


def _grid_model(grid: Grid) -> GridModel:
    return GridModel(
        margin_left=grid.margin_left, margin_right=grid.margin_right,
        margin_top=grid.margin_top, margin_bottom=grid.margin_bottom,
        columns=[
            ColumnAxisModel(center=c.center, count=c.count, confidence=c.confidence, source=c.source)
            for c in grid.columns
        ],
        gutter=grid.gutter, anchors=dict(grid.anchors), confidence=dict(grid.confidence),
        skipped_no_box=grid.skipped_no_box, native_guides_used=grid.native_guides_used,
    )


class BoxModel(BaseModel):
    left: float
    top: float
    width: float
    height: float


def _box_model(box) -> BoxModel:
    return BoxModel(left=box.left, top=box.top, width=box.width, height=box.height)


class BackgroundModel(BaseModel):
    color: ColorValue | None
    source: str
    luminance: float | None


def _background_model(bg: Background) -> BackgroundModel:
    return BackgroundModel(color=_color_value(bg.color), source=bg.source, luminance=bg.luminance)


class PlaceholderSlotModel(BaseModel):
    ph_type: str
    box: BoxModel


class LayoutEntryModel(BaseModel):
    layout_id: str
    part_name: str
    name: str
    master_index: int
    kind: str
    kind_confidence: float
    background: BackgroundModel
    is_dark: bool
    placeholders: list[PlaceholderSlotModel]
    decor_count: int
    asset_refs: list[str]
    usage_count: int


def _layout_entry_model(entry: LayoutEntry) -> LayoutEntryModel:
    return LayoutEntryModel(
        layout_id=entry.layout_id, part_name=entry.part_name, name=entry.name,
        master_index=entry.master_index, kind=entry.kind, kind_confidence=entry.kind_confidence,
        background=_background_model(entry.background), is_dark=entry.is_dark,
        placeholders=[
            PlaceholderSlotModel(ph_type=p.ph_type, box=_box_model(p.box)) for p in entry.placeholders
        ],
        decor_count=entry.decor_count, asset_refs=list(entry.asset_refs), usage_count=entry.usage_count,
    )


class PlacementModel(BaseModel):
    part_name: str
    part_kind: str
    box: BoxModel
    from_bg: bool
    under_content: bool


def _placement_model(p: Placement) -> PlacementModel:
    return PlacementModel(
        part_name=p.part_name, part_kind=p.part_kind, box=_box_model(p.box),
        from_bg=p.from_bg, under_content=p.under_content,
    )


class AssetRefModel(BaseModel):
    part_name: str
    width: int | None
    height: int | None
    size_bytes: int
    has_alpha: bool
    square: bool
    placements: list[PlacementModel]
    confidence: float
    reason: str | None


def _asset_ref_model(ref: AssetRef) -> AssetRefModel:
    return AssetRefModel(
        part_name=ref.part_name, width=ref.width, height=ref.height, size_bytes=ref.size_bytes,
        has_alpha=ref.has_alpha, square=ref.square,
        placements=[_placement_model(p) for p in ref.placements],
        confidence=ref.confidence, reason=ref.reason,
    )


class AssetCatalogModel(BaseModel):
    logo: AssetRefModel | None
    logo_placements: list[PlacementModel]
    backgrounds: list[AssetRefModel]
    icons: list[AssetRefModel]
    photos: list[AssetRefModel]
    unclassified: list[AssetRefModel]
    boxless_placements: int
    logo_reason: str | None


def _asset_catalog_model(catalog: AssetCatalog) -> AssetCatalogModel:
    return AssetCatalogModel(
        logo=_asset_ref_model(catalog.logo) if catalog.logo is not None else None,
        logo_placements=[_placement_model(p) for p in catalog.logo_placements],
        backgrounds=[_asset_ref_model(a) for a in catalog.backgrounds],
        icons=[_asset_ref_model(a) for a in catalog.icons],
        photos=[_asset_ref_model(a) for a in catalog.photos],
        unclassified=[_asset_ref_model(a) for a in catalog.unclassified],
        boxless_placements=catalog.boxless_placements, logo_reason=catalog.logo_reason,
    )


class PatternSlotModel(BaseModel):
    role: str
    box: BoxModel
    size_pt: float
    color_hex: str | None
    align: str
    max_chars: int
    wraps: bool
    sample_text: str | None
    # Вертикальное выравнивание текста в рамке, снятое со слайда-примера
    # (`patterns.PatternSlot.anchor`). Значение по умолчанию — обратная
    # совместимость со старым кешем.
    anchor: str = "t"
    # Схема места от модели (`patterns.PatternSlot.purpose` и соседние,
    # см. их комментарий). Пустые значения: модель не спрашивали.
    purpose: str | None = None
    content_hint: str | None = None
    max_words: int | None = None
    ordinal: bool = False
    fixed: bool = False
    schema_confidence: float | None = None
    # Id исходной фигуры на слайде-примере (`patterns.PatternSlot.
    # source_shape_id`). `None`: старый кеш или фигура без id, клон тогда
    # ищет фигуру по коробке.
    source_shape_id: str | None = None


# Поля схемы места: одним списком для применения ответа модели и для
# переноса схемы между зеркалами при перемайнинге (`_carry_slot_schema`).
_SLOT_SCHEMA_FIELDS = ("purpose", "content_hint", "max_words", "ordinal", "fixed", "schema_confidence")


def _pattern_slot_model(slot: PatternSlot) -> PatternSlotModel:
    return PatternSlotModel(
        role=slot.role, box=_box_model(slot.box), size_pt=slot.size_pt, color_hex=slot.color_hex,
        align=slot.align, max_chars=slot.max_chars, wraps=slot.wraps, sample_text=slot.sample_text,
        anchor=slot.anchor, purpose=slot.purpose, content_hint=slot.content_hint,
        max_words=slot.max_words, ordinal=slot.ordinal, fixed=slot.fixed,
        schema_confidence=slot.schema_confidence, source_shape_id=slot.source_shape_id,
    )


class RepeatSpecModel(BaseModel):
    axis: str
    count: int
    step: float
    slot_roles: list[str]
    group_size: int


def _repeat_spec_model(repeat: RepeatSpec | None) -> RepeatSpecModel | None:
    if repeat is None:
        return None
    return RepeatSpecModel(
        axis=repeat.axis, count=repeat.count, step=repeat.step,
        slot_roles=list(repeat.slot_roles), group_size=repeat.group_size,
    )


class CapacityModel(BaseModel):
    max_items: int
    max_chars_per_item: int
    max_bullets: int
    max_series: int
    max_rows: int
    max_cols: int


def _capacity_model(cap: Capacity) -> CapacityModel:
    return CapacityModel(
        max_items=cap.max_items, max_chars_per_item=cap.max_chars_per_item, max_bullets=cap.max_bullets,
        max_series=cap.max_series, max_rows=cap.max_rows, max_cols=cap.max_cols,
    )


class DecorShapeModel(BaseModel):
    kind: str
    box: BoxModel
    rotation: float
    flip_h: bool
    flip_v: bool
    fill_hex: str | None
    has_fill: bool
    fill_kind: str
    # Task 9 повторное ревью, находка №1: принадлежность декора группе
    # повтора текстовых слотов (`repeat_index` — позиция в группе) — без
    # этих полей `compose/` нечем отличить декор, который обязан
    # развернуться вместе с текстом (`compose.blocks.expand_decor`), от
    # самостоятельного украшения. Значения по умолчанию — обратная
    # совместимость со старым диск-кешем профиля (см. `TemplateProfile.
    # from_file`, docstring про "повреждённый кеш не роняет вызов"): старая
    # запись без этих полей валидируется как "декор вне группы повтора",
    # что и было её фактическим поведением до этой правки.
    repeat_group: bool = False
    repeat_index: int = 0
    # Часть пакета с картинкой (`ppt/media/imageN.png`) для `kind ==
    # "picture"` — без неё картиночный декор нечем нарисовать, и фирменная
    # графика шаблона терялась целиком (см. `patterns.DecorShape.
    # image_part`). Значение по умолчанию — обратная совместимость со
    # старым диск-кешем, тем же приёмом, что и поля повтора выше.
    image_part: str | None = None
    # Текст значка — короткая подпись внутри залитой фигуры (номер шага,
    # буква). См. `patterns.DecorShape.badge_text`.
    badge_text: str | None = None
    badge_size_pt: float = 0.0
    badge_color_hex: str | None = None
    # Пресет-форма автофигуры (`a:prstGeom`, например `ellipse`): без неё
    # круглый значок шаблона рисуется квадратом.
    prst: str | None = None
    # Id исходной фигуры на слайде-примере (`patterns.DecorShape.
    # source_shape_id`): по нему клон убирает декор незаполненных единиц.
    source_shape_id: str | None = None


def _decor_shape_model(decor: DecorShape) -> DecorShapeModel:
    return DecorShapeModel(
        kind=decor.kind, box=_box_model(decor.box), rotation=decor.rotation,
        flip_h=decor.flip_h, flip_v=decor.flip_v, fill_hex=decor.fill_hex,
        has_fill=decor.has_fill, fill_kind=decor.fill_kind,
        repeat_group=decor.repeat_group, repeat_index=decor.repeat_index,
        image_part=decor.image_part, badge_text=decor.badge_text,
        badge_size_pt=decor.badge_size_pt, badge_color_hex=decor.badge_color_hex,
        prst=decor.prst, source_shape_id=decor.source_shape_id,
    )


class PatternModel(BaseModel):
    pattern_id: str
    source_slide_index: list[int]
    layout_id: str
    kind: str
    slots: list[PatternSlotModel]
    repeat: RepeatSpecModel | None
    decor: list[DecorShapeModel]
    capacity: CapacityModel
    score: float
    is_dark: bool
    # Задача "разбор незнакомого шаблона в бюджет": уверенность
    # ГЕОМЕТРИЧЕСКОГО классификатора в своём `kind` (см. докстроку
    # `patterns.Pattern.kind_confidence`/`patterns._classify_kind`) —
    # `template.vision_kind.classify_patterns_by_vision` спрашивает модель
    # только про паттерны ниже порога уверенности, не про все. Дефолт `1.0`
    # — обратная совместимость со старым диск-кешем без этого поля (тот же
    # принцип, что и у `RepeatSpecModel.group_size`/`DecorShapeModel.
    # repeat_group` выше в этом файле).
    kind_confidence: float = 1.0
    # Задача C ("превью PNG на каждый паттерн шаблона в кэше профиля") —
    # путь к PNG первого исходного слайда паттерна (`source_slide_index[0]`),
    # ОТНОСИТЕЛЬНЫЙ от каталога профиля в кеше (`<cache_dir>/<fingerprint>/`,
    # см. `_save_pattern_previews` и `TemplateProfile.pattern_preview_path`)
    # — не абсолютный, чтобы кеш, скопированный/перенесённый на другую
    # машину, не нёс путей чужого диска. `None`, если превью не сохранялось
    # (нет soffice/poppler на машине разбора, разбор без ключа `vision`
    # вовсе — см. докстроку `_save_pattern_previews`, честная деградация,
    # тот же принцип, что и у остальных опциональных источников профиля).
    preview_path: str | None = None
    # Задача R: заполненность слайда-примера (`patterns.Pattern.
    # source_density`), с ней D05 сравнивает клон. `None` у моделей,
    # собранных без майнинга (тестовые фикстуры): D05 тогда судит по
    # глобальному коридору.
    source_density: float | None = None


def _pattern_model(pattern: Pattern, preview_path: str | None = None) -> PatternModel:
    return PatternModel(
        pattern_id=pattern.pattern_id, source_slide_index=list(pattern.source_slide_index),
        layout_id=pattern.layout_id, kind=pattern.kind,
        slots=[_pattern_slot_model(s) for s in pattern.slots],
        repeat=_repeat_spec_model(pattern.repeat),
        decor=[_decor_shape_model(d) for d in pattern.decor],
        capacity=_capacity_model(pattern.capacity), score=pattern.score, is_dark=pattern.is_dark,
        kind_confidence=pattern.kind_confidence, preview_path=preview_path,
        source_density=pattern.source_density,
    )


def _apply_slot_schema(
    models: list[PatternModel], schema: dict[str, dict[int, dict]],
) -> list[PatternModel]:
    """Кладёт принятую схему мест (`vision_kind.describe_pattern_slots`) в
    зеркала паттернов и пересчитывает `max_chars_per_item` той же функцией,
    что и майнинг (`patterns.chars_per_item`): иначе ранжир раскладок
    продолжал бы обещать писателю абзац там, где модель видит три слова."""
    result = []
    for model in models:
        entries = schema.get(model.pattern_id)
        if not entries:
            result.append(model)
            continue
        slots = [
            slot.model_copy(update=entries[i]) if i in entries else slot
            for i, slot in enumerate(model.slots)
        ]
        capacity = model.capacity.model_copy(update={"max_chars_per_item": chars_per_item(slots)})
        result.append(model.model_copy(update={"slots": slots, "capacity": capacity}))
    return result


def _slot_schema_of(models: list[PatternModel]) -> dict[str, dict[int, dict]]:
    """Схема мест, уже лежащая в зеркалах: то же представление, что отдаёт
    `describe_pattern_slots`. Нужна, когда паттерны перемайниваются заново
    (`_reclassify_pattern_kinds`), а схема от модели уже есть в кеше и
    терять её нельзя."""
    defaults = PatternSlotModel.model_fields
    schema: dict[str, dict[int, dict]] = {}
    for model in models:
        for i, slot in enumerate(model.slots):
            fields = {
                name: getattr(slot, name) for name in _SLOT_SCHEMA_FIELDS
                if getattr(slot, name) != defaults[name].default
            }
            if fields:
                schema.setdefault(model.pattern_id, {})[i] = fields
    return schema


# ---------------------------------------------------------------------------
# TemplateProfile
# ---------------------------------------------------------------------------

# Часовой для "параметр `cache_dir` не передан вовсе" — отличим от
# `cache_dir=None`, переданного ЯВНО. Найдено этой задачей (Task 18): до
# этой правки `from_file` не различал "не передан" и "передан None" (оба
# читались как `cache_dir if cache_dir is not None else _default_cache_dir()`
# — то есть `cache_dir=None` молча ПОДМЕНЯЛСЯ каталогом кеша по умолчанию,
# а не отключал кеш, как ожидали ЧЕТЫРЕ conftest.py (`tests/plan/`,
# `tests/compose/`, `tests/render/`, `tests/export/`) — все они зовут
# `from_file(TEMPLATE, cache_dir=None)`, рассчитывая на свежий, изолированный
# от диск-кеша профиль. Пока `vision`/`namer` не меняли `Pattern.kind`
# (до Task 18), эта путаница была безвредна — кеш-хит с чужого прогона нёс
# ТЕ ЖЕ геометрические паттерны, что и свежий разбор. С Task 18 `Pattern.
# kind` в кеше может отличаться от геометрии (вид уточнён моделью в чужом
# прогоне `deckforge parse`/`generate` с ключом) — `tests/export/test_html.
# py::test_html_slides_are_text_not_screenshots` реально поймал это на живом
# прогоне обязательной проверки этой задачи: `_profile()` получил ИЗ
# ОБЩЕГО кеша профиль с раскладками, `kind` которых сменила модель, и
# `build_deck` положил тестовый контент на другую, не ту раскладку, под
# которую написан хардкодный `DeckSpec` теста. Теперь `cache_dir=None`,
# переданный ЯВНО, действительно отключает кеш (`effective_cache_dir`
# остаётся `None`) — так, как и предполагали все четыре conftest.py с
# самого начала.
_CACHE_DIR_UNSET = object()


def _default_cache_dir() -> Path | None:
    """Каталог диск-кеша профилей по умолчанию — `paths.profile_cache` из
    `config/app.yaml` (единственная точка настройки, как и всё остальное в
    проекте). `None`, если конфиг не читается вовсе (файла нет, YAML битый,
    секрет протёк в yaml и т.п.) — кеш в этом случае просто не используется,
    `from_file` разбирает шаблон как обычно, это не должно ронять вызов."""
    try:
        settings = Settings.load(APP_YAML_PATH)
    except Exception:
        return None
    return settings.paths.profile_cache


def _default_preview_dpi() -> int:
    """dpi рендера превью паттернов — та же настройка, что уже откалибрована
    для уточнения вида раскладки моделью (`config/app.yaml`, `render.
    pattern_kind_dpi`, см. её докстроку в `settings.py` про живой замер).
    Отдельная, более высокая настройка только ради превью не заводится
    (Задача C, брифом прямо: "не выше: разбор и так ~190 с") — превью нужно
    человеку для доверия к разбору, не для печати."""
    try:
        return Settings.load(APP_YAML_PATH).render.pattern_kind_dpi
    except Exception:
        return 72  # тот же запасной dpi, что и у vision_kind.DEFAULT_RENDER_DPI


def _render_pattern_pngs(patterns, template_path: Path) -> dict[str, bytes]:
    """PNG первого исходного слайда каждого паттерна, `pattern_id -> байты`.
    Один рендер на превью в кеше и на схему слотов от модели: рендер
    шаблона стоит десятки секунд, платить его дважды незачем. Принимает и
    `Pattern`, и `PatternModel` (нужны `pattern_id` и `source_slide_index`).
    Никогда не бросает: нет soffice, рендер упал — пустой словарь."""
    needed_pages = sorted({p.source_slide_index[0] for p in patterns if p.source_slide_index})
    if not needed_pages:
        return {}
    try:
        with tempfile.TemporaryDirectory(prefix="deckforge-pattern-previews-") as tmp_dir:
            try:
                pngs = to_pngs(template_path, Path(tmp_dir), dpi=_default_preview_dpi(), pages=needed_pages)
            except RenderError:
                return {}
            # `to_pngs(..., pages=needed_pages)` возвращает по одному PNG на
            # страницу, по возрастанию номера (её докстрока), `needed_pages`
            # отсортирован и без дублей: позиционное сопоставление верно.
            if len(pngs) != len(needed_pages):
                return {}
            by_page = {page: png.read_bytes() for page, png in zip(needed_pages, pngs)}
    except OSError:
        return {}
    return {
        p.pattern_id: by_page[p.source_slide_index[0]]
        for p in patterns
        if p.source_slide_index and p.source_slide_index[0] in by_page
    }


def _save_pattern_previews(
    patterns: list[Pattern], template_path: Path, preview_dir: Path | None,
    pngs: dict[str, bytes] | None = None,
) -> dict[str, str]:
    """Рендерит PNG первого исходного слайда (`Pattern.source_slide_index[0]`)
    КАЖДОГО паттерна и сохраняет их рядом с JSON профиля в кеше
    (`<cache_dir>/<fingerprint>/previews/<pattern_id>.png`, см. `from_file`
    и `TemplateProfile.pattern_preview_path`) — задача C ("превью PNG на
    каждый паттерн шаблона в кэше профиля"): без них жюри и разработчику
    нечем увидеть, что реально снято с шаблона, кроме чтения JSON профиля
    целиком.

    Возвращает `pattern_id -> относительный путь` (`"previews/<id>.png"`,
    от каталога профиля в кеше, не от `preview_dir` буквально — см.
    докстроку `PatternModel.preview_path`) только для паттернов, чьё превью
    реально сохранилось; остальные (страница не нашлась, запись на диск не
    удалась) в словарь не попадают — вызывающий код читает отсутствие ключа
    как `preview_path=None`.

    `preview_dir` — `None`, если сохранять превью решительно некуда (кеш
    профиля выключен вызывающим кодом, `cache_dir=None` явно, или ключ
    `vision` не передан вовсе — см. докстроку `from_file`, "честная
    деградация": рендер шаблона не бесплатен, а без модели профиль обязан
    собираться быстро, как и раньше). НИКОГДА не бросает исключение наружу:
    нет soffice/poppler на машине, рендер завис/упал — тот же принцип
    честной деградации, что у `vision_kind.classify_patterns_by_vision`
    (пустой словарь, разбор профиля продолжается как есть)."""
    if preview_dir is None or not patterns:
        return {}
    if pngs is None:
        pngs = _render_pattern_pngs(patterns, template_path)
    if not pngs:
        return {}
    try:
        preview_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    mapping: dict[str, str] = {}
    for pattern in patterns:
        data = pngs.get(pattern.pattern_id)
        if data is None:
            continue
        try:
            (preview_dir / f"{pattern.pattern_id}.png").write_bytes(data)
        except OSError:
            continue
        mapping[pattern.pattern_id] = f"previews/{pattern.pattern_id}.png"
    return mapping


def _previews_and_slot_schema(
    patterns: list[Pattern], template_path: Path, preview_dir: Path | None,
    schema_llm: VisionProvider | None,
) -> tuple[dict[str, str], dict[str, dict[int, dict]], list[str], bool]:
    """Превью в кеш и схема слотов от модели одним рендером. Возвращает
    (превью, схема, заметки схемы, снималась ли схема). Без модели и без
    каталога превью ничего не рендерит (разбор без ключа обязан оставаться
    быстрым, см. `test_parsing_is_fast_enough`)."""
    if preview_dir is None and schema_llm is None:
        return {}, {}, [], False
    pngs = _render_pattern_pngs(patterns, template_path)
    previews = _save_pattern_previews(patterns, template_path, preview_dir, pngs=pngs)
    if schema_llm is None:
        return previews, {}, [], False
    if not pngs:
        return previews, {}, ["Схема слотов не снималась: рендер слайдов-примеров не удался."], False
    schema, notes = describe_pattern_slots(patterns, pngs, schema_llm)
    return previews, schema, notes, True


# Начало строк провенанса про схему слотов (сводка `describe_pattern_slots`
# и её отказ целиком) — по нему строка заменяется при дозапросе на кеш-хите.
_SCHEMA_PROVENANCE_PREFIX = "Схема слотов"

# Часовой для «`schema` не передан»: тогда схему снимает тот же
# мультимодальный провайдер, что и вид раскладки (`vision`). Так её
# получают все, кто уже передаёт `vision` (веб-сервис в том числе), а
# `cli.py` может отдать роли `pattern_schema` свою модель из конфига.
_SCHEMA_FROM_VISION = object()


class TemplateProfile(BaseModel):
    """Единый объект дизайн-системы шаблона — интерфейс брифа (Step 1)
    дословно (`.from_file`, `.to_json`, `.fingerprint`, `.provenance`,
    `.warnings`) плюс сами данные дизайн-системы, без которых генерации
    и аудиту, работающим по профилю (следующие задачи), нечем пользоваться."""

    source_name: str
    # Task 10: путь к файлу шаблона на момент разбора (абсолютный,
    # `Path.resolve()`) — единственное, чего не хватало downstream-коду
    # (`compose/diagrams.py::add_pictogram_row`), чтобы вставить в новый
    # слайд РЕАЛЬНЫЕ байты иконки шаблона: `TemplateProfile` — pydantic-
    # модель из примитивов JSON (докстрока выше), сырых байт media в ней
    # нет и не будет, а `source_name` (голое имя файла) недостаточно, чтобы
    # найти файл на диске повторно. Портируется только пока файл шаблона не
    # переехал на диске — честное ограничение, не хуже прежнего отсутствия
    # пути вовсе (раньше вставить иконку из шаблона в новый слайд было
    # нечем).
    source_path: str = ""
    canvas_width_emu: int
    canvas_height_emu: int
    theme: ThemeModel
    theme_part: str
    master_part: str
    palette_roles: dict[str, str]
    palette_roles_source: str = "fallback"
    type_scale: TypeScaleModel
    grid: GridModel
    layouts: list[LayoutEntryModel]
    assets: AssetCatalogModel
    patterns: list[PatternModel]
    # Уточнялся ли вид раскладки (`PatternModel.kind`) моделью при сборке
    # ЭТОГО профиля: "model" — `vision` был передан и `classify_patterns_by_
    # vision` отработала, "geometry" — модель не звалась вовсе, виды сняты
    # только геометрией (`patterns._classify_kind`). Зеркало `palette_roles_
    # source` и нужен ровно за тем же: ключ диск-кеша — отпечаток ФАЙЛА
    # шаблона, он ничего не знает про то, был ли ключ модели у прогона,
    # который этот профиль записал. Без этого признака профиль, собранный
    # без ключа, навсегда отдавался бы и тем вызовам, у которых ключ есть
    # (найдено на живом прогоне: 8 из 15 раскладок ЛЦТ2026 остались
    # `bullets` с `kind_confidence=0.3` — корзина по умолчанию, обязанная
    # уйти на уточнение моделью). См. `from_file`/`_reclassify_pattern_kinds`.
    #
    # Честная оговорка про смысл "model": это «модель спрашивали», а не
    # «модель ответила». Если рендер или сеть подвели, виды остаются
    # геометрическими (отказ виден в `.warnings`), но перезапрашиваться на
    # каждом чтении кеша не будут — рендер шаблона стоит десятки секунд, и
    # платить их на каждой генерации ради повторной попытки дороже, чем
    # один раз почистить `cache/profiles/`. У `palette_roles_source`
    # выбран противоположный компромисс ("fallback" после неудачи модели →
    # попытка повторяется), потому что именование палитры не требует
    # рендера и стоит одного текстового вызова.
    pattern_kinds_source: str = "geometry"
    # Снималась ли схема слотов моделью (`PatternSlotModel.purpose` и
    # соседние): "model" — да, "none" — нет (ключа не было или рендер не
    # удался). Зачем, см. `pattern_kinds_source`: ключ кеша не знает, был ли
    # у записавшего прогона ключ модели, и без признака профиль без схемы
    # навсегда отдавался бы и прогонам, способным её снять.
    pattern_schema_source: str = "none"
    # Task 10: словарь автофигур шаблона (`ShapeVocabEntry`, по убыванию
    # частоты) — `compose/diagrams.py` рисует карточки схем ТОЛЬКО формами
    # из этого списка (см. докстроку `template/shapes.py`), никогда не
    # придумывая скругление, которого нет в шаблоне.
    shape_vocabulary: list[ShapeVocabEntryModel] = []
    # Task 10: явная палитра рядов графика — цветные токены шаблона по
    # весу, достроенные оттенками `brand` (см. `template/chart_palette.py`).
    # `compose/charts.py` красит каждый ряд/точку ТОЛЬКО этими цветами —
    # без этого `python-pptx` отдаёт раскраску стоковой теме Office.
    chart_series: list[str] = []
    provenance: list[str]
    warnings: list[str]
    fingerprint: str
    schema_version: int = PROFILE_SCHEMA_VERSION

    @classmethod
    def from_file(
        cls, path: Path, *, namer: LLMProvider | None = None, vision: VisionProvider | None = None,
        cache_dir: Path | None = _CACHE_DIR_UNSET,  # type: ignore[assignment]
        schema: VisionProvider | None = _SCHEMA_FROM_VISION,  # type: ignore[assignment]
    ) -> "TemplateProfile":
        """Разбирает `.pptx`-шаблон целиком, ровно один проход по пакету.

        `vision` (Task 18) — мультимодальный провайдер для уточнения
        `Pattern.kind` показом картинки слайда-примера (`template.
        vision_kind.classify_patterns_by_vision`) — необязательный, той же
        честной деградацией, что и `namer`: без него (или при сбое рендера/
        сети/ответа модели) `Pattern.kind` остаётся ровно тем, что снял
        геометрический майнинг `patterns.mine_patterns`, разбор не падает и
        не замедляется рендером шаблона. Кеш-хит с видами, снятыми ТОЛЬКО
        геометрией (`pattern_kinds_source != "model"`), при переданном
        `vision` дозапрашивается — см. `_reclassify_pattern_kinds` ниже и
        абзац про ключ кеша. До этой правки такой кеш-хит отдавался как есть
        навсегда, и разбор, сделанный один раз без ключа модели, навсегда
        отравлял шаблон: на ЛЦТ2026 восемь из пятнадцати раскладок остались
        `bullets` с `kind_confidence=0.3` (корзина по умолчанию, обязанная
        по порогу `vision_kind._ASK_CONFIDENCE_THRESHOLD` уйти на уточнение
        моделью), хотя ключ у прогона был.

        Порядок и переиспользование посчитанного повторяют
        `tests/template/conftest.py::_build_profile` (прообраз сборки,
        на котором уже стоят тесты Task 4-7) — `usage`/`type_scale`
        передаются в `build_layout_catalog` готовыми, а не пересчитываются
        внутри него (бюджет времени задачи — тяжёлые функции не вызываются
        дважды).

        Диск-кеш по отпечатку файла (Task 8 код-ревью, находка 2): если файл
        по пути `path` не менялся (тот же sha256, см. `.fingerprint`) и в
        каталоге кеша уже лежит профиль с этим отпечатком, ВЫЗЫВАЮЩАЯ
        СТОРОНА ПОЛУЧАЕТ ГОТОВЫЙ ПРОФИЛЬ ИЗ КЕША — ни разбор пакета, ни
        (если он был бы нужен) сетевой вызов именования палитры/вида
        раскладки не повторяются. Каталог кеша — `cache_dir`, если параметр
        передан вовсе (ЛЮБЫМ значением, включая `None` — см. `_CACHE_DIR_
        UNSET` ниже, Task 18: `cache_dir=None`, переданный ЯВНО, ОТКЛЮЧАЕТ
        кеш полностью, а не подменяется каталогом по умолчанию, как было до
        этой правки), иначе (параметр вовсе не указан вызывающим кодом) —
        `paths.profile_cache` из `config/app.yaml` (см. `_default_cache_dir`
        ниже); если конфиг недоступен или каталог кеша по умолчанию не
        настроен — кеш просто не используется, разбор идёт как обычно (кеш —
        оптимизация повторного вызова на одном и том же файле, а не
        обязательное условие сборки профиля). Повреждённый файл кеша не
        роняет вызов — профиль пересчитывается заново и перезаписывает его.

        Ключ кеша — только отпечаток ФАЙЛА, без учёта `namer`/`vision`
        (Task 8 код-ревью, находка 2). Сам по себе он не различает разбор,
        сделанный с моделью, и разбор, сделанный без неё, — поэтому профиль
        несёт ДВА признака происхождения своих «модельных» частей
        (`palette_roles_source`, `pattern_kinds_source`), и кеш-хит
        проверяется по ним, а не по одному лишь отпечатку:

        - сейчас передан `namer`, а роли в кеше — запасной вариант
          (`palette_roles_source != "model"`): роли переназначаются моделью
          (`_reassign_palette_roles`);
        - сейчас передан `vision`, а виды раскладок в кеше сняты только
          геометрией (`pattern_kinds_source != "model"`): паттерны
          перемайниваются (детерминированно, без сети) и уходят на уточнение
          модели (`_reclassify_pattern_kinds`);
        - обновлённый профиль перезаписывает файл кеша, чтобы следующий
          вызов с той же конфигурацией ничего не дозапрашивал.

        Обратный случай — в кеше уже ответ модели, а вызов идёт без ключа —
        отдаётся как есть: уже полученное от модели не деградирует до
        запасного варианта/голой геометрии, и ничего не перечитывается."""
        path = Path(path)
        # Ключ несёт ВЕРСИЮ разбора вместе с отпечатком файла (`store.
        # profile_key`). Сверка версии внутри записи ниже остаётся — она
        # ловит записи, сделанные до появления версионного ключа, — но
        # основную защиту даёт именно ключ: код новой версии просто не
        # видит чужих записей, в том числе в общем хранилище, которое
        # переживает выкатку (см. докстроку `template/store.py`).
        # `schema` (задача F) — провайдер схемы слотов, см. `_SCHEMA_FROM_
        # VISION`: не передан — тот же, что `vision`; `None` явно — схема не
        # снимается.
        schema_llm = vision if schema is _SCHEMA_FROM_VISION else schema
        fingerprint = profile_key(path.read_bytes(), PROFILE_SCHEMA_VERSION)

        # Найдено этой задачей: `cache_dir` объявлен типом `Path | None`, но
        # Python не приводит аргументы к аннотации сама — вызывающий код,
        # передавший ГОЛУЮ СТРОКУ (`cache_dir="некоторый/путь"`), раньше
        # долетал до `effective_cache_dir / f"{fingerprint}.json"` ниже и
        # падал `TypeError: unsupported operand type(s) for /: 'str' and
        # 'str'` — деталь реализации (что каталог кеша собирается через `/`)
        # протекала наружу как малопонятная ошибка типа, а не как честная
        # работа с любым путём. `Path(...)` тут же приводит строку к пути
        # (и не портит уже-Path, `Path(Path(x)) == Path(x)`), `_CACHE_DIR_
        # UNSET`/`None` пропускаются мимо приведения как есть — оба не несут
        # осмысленного пути, приводить их в `Path` незачем и, для `None`,
        # ломало бы дальнейшую проверку `is not None` ниже.
        effective_cache_dir = cache_dir if cache_dir is not _CACHE_DIR_UNSET else _default_cache_dir()
        if effective_cache_dir is not None:
            effective_cache_dir = Path(effective_cache_dir)
        cache_file = (
            effective_cache_dir / f"{fingerprint}.json" if effective_cache_dir is not None else None
        )
        if cache_file is not None and cache_file.exists():
            cached: "TemplateProfile | None" = None
            try:
                raw_text = cache_file.read_text(encoding="utf-8")
                raw_data = json.loads(raw_text)
                # Версия схемы сверяется НА СЫРОМ словаре, ДО pydantic-
                # валидации (см. докстроку `PROFILE_SCHEMA_VERSION`) —
                # если бы поле просто имело дефолт в модели, кеш, в
                # котором его нет вовсе (записан до появления этой
                # версии), тихо прошёл бы валидацию С ДЕФОЛТНЫМ (текущим)
                # значением, как будто он и есть текущая версия — ровно та
                # деградация, которую эта проверка обязана ловить.
                if isinstance(raw_data, dict) and raw_data.get("schema_version") == PROFILE_SCHEMA_VERSION:
                    cached = cls.model_validate(raw_data)
            except Exception:
                cached = None  # повреждённый кеш — разбираем заново и перезаписываем ниже
            if cached is not None:
                refreshed = False
                if namer is not None and cached.palette_roles_source != "model":
                    # Честная оговорка из докстроки выше: ключ кеша — только
                    # отпечаток файла, без учёта namer. Профиль мог лечь в
                    # кеш без ключа (роли — запасным вариантом) раньше, чем
                    # ключ появился. Раз модель сейчас доступна и роли ещё не
                    # от неё — переназначаем роли моделью и обновляем кеш, не
                    # трогая остальной (детерминированный, не изменившийся)
                    # разбор — см. `_reassign_palette_roles`.
                    cached = cls._reassign_palette_roles(cached, path, namer)
                    refreshed = True
                if vision is not None and cached.pattern_kinds_source != "model":
                    # То же самое, но про вид раскладки: профиль мог лечь в
                    # кеш в прогоне без ключа `vision` (виды — только
                    # геометрия), а сейчас модель доступна. Такой профиль не
                    # годится для запроса, у которого модель есть, — иначе
                    # половина шаблона остаётся в корзине по умолчанию
                    # навсегда (см. докстроку `pattern_kinds_source`).
                    cached = cls._reclassify_pattern_kinds(cached, path, vision, effective_cache_dir)
                    refreshed = True
                if schema_llm is not None and cached.pattern_schema_source != "model":
                    # После видов: тот дозапрос перемайнивает паттерны и
                    # кладёт свежие превью, которые здесь читаются с диска.
                    cached = cls._describe_cached_slots(cached, path, schema_llm, effective_cache_dir)
                    refreshed = True
                if refreshed and cache_file is not None:
                    # Запись одна на оба дозапроса — иначе профиль, которому
                    # нужны и роли, и виды, лёг бы в кеш дважды, причём
                    # первый раз в промежуточном состоянии.
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_text(cached.to_json(), encoding="utf-8")
                    except OSError:
                        pass  # кеш — оптимизация, не обязана быть надёжной
                return cached

        with PptxPackage.open(path) as pkg:
            canvas = pkg.canvas()
            usage = collect_usage(pkg, canvas)
            type_scale = build_type_scale(pkg, canvas, usage)
            grid = build_grid(pkg, canvas)
            master_part = pick_primary_master(pkg)
            theme_for_layouts = read_theme(pkg, master_part)
            theme_part = pkg.related(master_part, "theme")[0]
            layouts = build_layout_catalog(
                pkg, canvas, theme_for_layouts, grid, usage=usage, type_scale=type_scale,
            )
            assets = build_asset_catalog(pkg, canvas, layouts)
            patterns = mine_patterns(pkg, canvas, grid, type_scale, assets)
            # Task 10 код-ревью, находка №1: словарь карточных форм считается
            # ПО ДЕКОРУ ГРУПП ПОВТОРА уже намайненных раскладок (`patterns`,
            # объект этого же прохода, до pydantic-сериализации — см.
            # докстроку `template/shapes.py`), не по переписи всех автофигур
            # пакета — макеты/мастера служебными рамками перевешивают язык
            # карточек, который реально использует шаблон.
            #
            # "Разбор незнакомого шаблона в бюджет", продолжение 3 —
            # НАРОЧНО посчитан здесь, ДО уточнения вида раскладки моделью
            # (`classify_patterns_by_vision` ниже, теперь вне блока `with`,
            # см. его комментарий), не после, как было раньше. Безопасно:
            # `build_shape_vocabulary`/`_card_decor_vocabulary` читают
            # только `Pattern.decor` (репит-группы) и `Pattern.slots`
            # (текстовые слоты) — ни одного обращения к `Pattern.kind` во
            # всём `template/shapes.py` нет (см. её докстроку), а
            # `classify_patterns_by_vision` меняет только `kind`
            # (`dataclasses.replace(p, kind=...)`), никогда `decor`/`slots`.
            # Результат этого вызова одинаков что до, что после уточнения
            # вида — переставить его раньше нужно ТОЛЬКО чтобы `pkg` можно
            # было закрыть до параллельного запуска именования палитры и
            # уточнения вида раскладки моделью ниже (обоим обращениям к
            # модели сам pkg не нужен, но `build_shape_vocabulary` — нужен, а
            # держать zip-пакет открытым во время сетевых вызовов моделей
            # незачем).
            shape_vocabulary = build_shape_vocabulary(pkg, canvas, patterns)

        # Тема для отчёта и именования палитры — уточнённая по фактическому
        # тексту слайдов (`Usage.primary_theme`, см. theme.
        # refine_font_scheme_degraded), не сырая `theme_for_layouts` выше:
        # каталог лейаутов исторически считается по сырой теме (см.
        # conftest.py), а отчёт человеку обязан отражать окончательный,
        # уточнённый вывод о деградации fontScheme.
        theme = usage.primary_theme or theme_for_layouts

        # "Разбор незнакомого шаблона в бюджет", продолжение 3 — именование
        # ролей палитры (`naming.name_palette_roles_report`) и уточнение
        # вида раскладки мультимодальной моделью
        # (`vision_kind.classify_patterns_by_vision`) — НЕЗАВИСИМЫЕ
        # обращения к модели: первое смотрит только на цвета уже
        # разобранного XML (`usage`/`theme`, посчитаны строками выше, `pkg`
        # им не нужен), второе — на отрисованные картинки слайдов-примеров
        # (сам открывает файл шаблона по `path` для рендера, `pkg` тоже не
        # нужен). Раньше шли по очереди, и живой замер ("Продолжение 2" в
        # task-18-report.md) отдельно измерил каждый шаг на контрольном
        # ЛЦТ2026: `namer` один — 60.8с, `vision` один — 121.8с, а вместе
        # (последовательно, как было) — 102.4-165.1с, то есть СУММА времени
        # обоих шагов почти без остатка, а не БОЛЬШЕЕ из двух. Запускаем
        # оба вызова из отдельных потоков одного пула (max_workers=2) —
        # должно остаться большее из двух шагов, не сумма.
        #
        # Почему это безопасно с дедлайном/эскалацией провайдера (проверено
        # чтением `provider/yandex.py`, не только надеждой): `namer` и
        # `vision` — РАЗНЫЕ инстансы `YandexProvider` (`cli.py::_build_
        # namer`/`_build_pattern_kind_vlm`, каждый свой вызов `_build_role_
        # provider`, свой `YandexProvider(...)`), у каждого свой `httpx.
        # Client`, свой `deadline_at` (отсчитывается ВНУТРИ `complete()`/
        # `ask_image()` от начала ИМЕННО ЭТОГО вызова — `self._now() +
        # self._deadline_seconds`, не общий таймер на оба провайдера) и
        # свой локальный `tried_budgets`/`attempt_counter` эскалации
        # (локальные переменные `_post_with_budget_escalation`, не атрибуты
        # инстанса, которые могли бы перепутаться между потоками). Между
        # двумя параллельными вызовами не разделяется НИЧЕГО мутируемого,
        # кроме `httpx.Client` каждого провайдера САМ С СОБОЙ (не друг с
        # другом) — а `httpx.Client` документирован как безопасный для
        # конкурентных запросов из нескольких потоков. Честная деградация
        # тоже не ломается: обе функции ниже уже НИКОГДА не бросают
        # исключение наружу штатным путём (см. их докстроки — обе сами
        # ловят сеть/парсинг и возвращают запасной вариант/геометрический
        # `kind` с заметкой об отказе) — сбой ОДНОГО потока не может
        # уронить `ThreadPoolExecutor` и не задерживает `.result()` второго.
        #
        # Задача C ("превью PNG на каждый паттерн шаблона в кэше профиля") —
        # третий поток того же пула, а не отдельный шаг ПОСЛЕ него: рендер
        # превью (`_save_pattern_previews`) — свой собственный `soffice`/
        # `pdftoppm` (докстрока `render/soffice.py`: ~40с на конвертацию в
        # PDF плюс растрирование нужных страниц), и, добавленный ПОСЛЕ
        # `namer`/`vision`, он бы прибавился к их и без того большому
        # времени (see vision_kind.py, "разбор незнакомого шаблона в
        # бюджет": уже 130-165с на контрольном шаблоне). Параллельно с ними
        # он почти бесплатен — прячется под большим из двух остальных шагов,
        # тот же приём, что уже применён здесь для `namer`/`vision`.
        # Единственный явный компромисс (см. докстроку `_save_pattern_
        # previews`): превью рендерятся, только если передан `vision`, —
        # без ключа модели `from_file` обязан собираться быстро, как и
        # раньше (`test_parsing_is_fast_enough`), а без `vision` (offline,
        # тесты) рендер шаблона и так не нужен ничему другому в этой
        # функции — заводить его ТОЛЬКО ради превью значило бы платить
        # десятки секунд там, где сейчас не платится ничего. `patterns` для
        # рендера — список ДО уточнения вида моделью (тот же объект, что
        # уйдёт в `vision_future`): `source_slide_index`/`pattern_id`
        # уточнение вида не трогает (`vision_kind.classify_patterns_by_
        # vision` меняет только `kind`, `dataclasses.replace`), поэтому
        # словарь превью остаётся верным для итогового списка.
        preview_dir = (
            effective_cache_dir / fingerprint / "previews"
            if effective_cache_dir is not None and vision is not None else None
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            vision_future = pool.submit(classify_patterns_by_vision, patterns, path, vision)
            palette_future = pool.submit(name_palette_roles_report, usage, theme, namer)
            # Задача F: превью и схема слотов одним рендером; схема идёт
            # параллельно с видом раскладки, а не после него, — у неё свой
            # пул потоков (`llm.pattern_schema_max_workers`), и разбор
            # шаблона не должен ждать их по очереди.
            preview_future = pool.submit(_previews_and_slot_schema, patterns, path, preview_dir, schema_llm)
            patterns, vision_notes = vision_future.result()
            palette_report = palette_future.result()
            preview_paths, slot_schema, schema_notes, schema_done = preview_future.result()

        chart_series = build_chart_series(usage, dict(palette_report.roles))

        provenance = _build_provenance(
            master_part=master_part, theme_part=theme_part, theme=theme, usage=usage,
            type_scale=type_scale, grid=grid, assets=assets, layouts=layouts,
            patterns=patterns, palette_notes=palette_report.notes, vision_notes=vision_notes,
        )
        warnings = _build_warnings(
            theme=theme, usage=usage, type_scale=type_scale, grid=grid,
            assets=assets, layouts=layouts, palette_notes=palette_report.notes,
        )
        # Заметки классификации вида раскладки моделью (см. `classify_
        # patterns_by_vision`) — ПЕРВАЯ строка (сводка "N из M") в provenance
        # (отчёт "откуда что взято" человеку), отказы отдельных паттернов
        # («слайд-источник не нашёлся», «модель предложила вид вне списка») —
        # в warnings, тем же признаком серьёзности, что и у остальных
        # деградировавших источников этого профиля (текстовое совпадение
        # достаточно здесь же, без отдельного класса заметок, как у
        # `PaletteNote.severity` — vision_notes уже сама решает, что писать
        # только в сводку, а что как отдельную строку отказа, см. её докстроку).
        warnings.extend(vision_notes[1:])
        provenance.extend(schema_notes[:1])
        warnings.extend(schema_notes[1:])

        profile = cls(
            source_name=path.name, source_path=str(path.resolve()),
            canvas_width_emu=canvas.width_emu, canvas_height_emu=canvas.height_emu,
            theme=_theme_model(theme), theme_part=theme_part, master_part=master_part,
            palette_roles=dict(palette_report.roles),
            palette_roles_source=palette_report.source,
            type_scale=_type_scale_model(type_scale), grid=_grid_model(grid),
            layouts=[_layout_entry_model(entry) for entry in layouts],
            assets=_asset_catalog_model(assets),
            patterns=_apply_slot_schema(
                [_pattern_model(p, preview_paths.get(p.pattern_id)) for p in patterns], slot_schema,
            ),
            pattern_schema_source="model" if schema_done else "none",
            # "model" значит «модель спрашивали», а не «модель ответила» —
            # см. комментарий у самого поля. Ответила она или отказала, видно
            # по `provenance`/`warnings` (заметки `classify_patterns_by_vision`).
            pattern_kinds_source="model" if vision is not None else "geometry",
            shape_vocabulary=[_shape_vocab_entry_model(e) for e in shape_vocabulary],
            chart_series=chart_series,
            provenance=provenance, warnings=warnings, fingerprint=fingerprint,
            schema_version=PROFILE_SCHEMA_VERSION,
        )

        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(profile.to_json(), encoding="utf-8")
            except OSError:
                pass  # кеш — оптимизация повторного вызова, не обязана быть надёжной

        return profile

    @classmethod
    def _reassign_palette_roles(
        cls, cached: "TemplateProfile", path: Path, namer: LLMProvider,
    ) -> "TemplateProfile":
        """Переназначает роли палитры моделью поверх кеш-хита с запасным
        вариантом (Task 8 код-ревью, находка 2) — единственный источник
        входа `.pptx`-пакета, который заново нужен наименованию ролей, это
        `usage`/`theme` (то же, что вычисляет `from_file` перед вызовом
        `name_palette_roles_report`, см. выше); типографика, сетка,
        лейауты, ассеты и паттерны из кеша не пересчитываются — они
        детерминированы и не изменились с первого разбора.

        Если и на этот раз модель не дала пригодного ответа (сеть/парсинг
        снова подвели — `palette_report.source` остаётся "fallback"),
        кешу всё равно можно записать обновлённые заметки об этой попытке;
        роли при этом не деградируют — `name_palette_roles_report` в этом
        случае сама возвращает тот же детерминированный запасной вариант."""
        with PptxPackage.open(path) as pkg:
            canvas = pkg.canvas()
            usage = collect_usage(pkg, canvas)
            theme = usage.primary_theme or read_theme(pkg, cached.master_part)

        palette_report = name_palette_roles_report(usage, theme, namer)
        # Task 10: палитра рядов графика зависит от того, какой цвет назван
        # "brand" (достройка оттенками, см. chart_palette.py) и какие роли
        # признаны нейтральными (фильтр из палитры рядов) — обе зависят от
        # ролей, которые здесь только что могли смениться с запасного
        # варианта на ответ модели, поэтому пересчитывается вместе с ними,
        # а не наследуется из кеша как есть.
        chart_series = build_chart_series(usage, dict(palette_report.roles))

        provenance = [
            line for line in cached.provenance if not line.startswith("Роли палитры: ")
        ]
        if palette_report.notes:
            provenance = provenance + [
                "Роли палитры: " + "; ".join(n.text for n in palette_report.notes) + "."
            ]
        warnings = [
            w for w in cached.warnings if not w.startswith("Именование ролей палитры: ")
        ]
        warnings = warnings + [
            f"Именование ролей палитры: {n.text}."
            for n in palette_report.notes if n.severity == "warning"
        ]

        return cached.model_copy(update={
            "palette_roles": dict(palette_report.roles),
            "palette_roles_source": palette_report.source,
            "chart_series": chart_series,
            "provenance": provenance,
            "warnings": warnings,
        })

    @classmethod
    def _reclassify_pattern_kinds(
        cls, cached: "TemplateProfile", path: Path, vision: VisionProvider,
        cache_dir: Path | None = None,
    ) -> "TemplateProfile":
        """Дозапрашивает вид раскладки у модели поверх кеш-хита, собранного
        без `vision` (виды — только геометрия `patterns._classify_kind`) —
        зеркало `_reassign_palette_roles` для второй «модельной» части
        профиля.

        `cache_dir` (задача C, "превью PNG на каждый паттерн шаблона в кэше
        профиля") — тот же `effective_cache_dir`, что уже посчитан в
        `from_file` до вызова этого метода: раз `vision` только что
        появился, у профиля впервые есть чем сделать превью (см. `_save_
        pattern_previews`), и честнее сделать это сразу, тем же рендером,
        что уже неизбежен для уточнения видов, а не оставлять `preview_
        path=None` до следующего полного разбора с нуля.

        Почему паттерны перемайниваются, а не берутся из кеша: `classify_
        patterns_by_vision` работает с дата-классами `patterns.Pattern`, а в
        профиле лежат их JSON-зеркала (`PatternModel`), причём зеркала
        неполные — `DecorShapeModel` не несёт `prst`/`adj`. Восстанавливать
        `Pattern` из зеркала значило бы собирать заведомо обеднённый объект
        и молча тащить это обеднение дальше при любой будущей правке
        `vision_kind.py`. Майнинг детерминированный и не ходит в сеть (живой
        замер разбора без моделей — меньше секунды против десятков секунд
        рендера и обращений к модели, ради которых всё и затевается), так
        что честнее посчитать паттерны заново.

        Остальной профиль (палитра, типографика, сетка, лейауты, ассеты,
        словарь форм, палитра рядов) из кеша не трогается: `classify_
        patterns_by_vision` меняет только `Pattern.kind`, а `shape_
        vocabulary` считается по `decor`/`slots` и от `kind` не зависит (см.
        докстроку `template/shapes.py`).

        Строка провенанса про вид раскладки заменяется целиком (старая —
        «не уточнялся моделью», новая — сводка модели), отказы отдельных
        паттернов добавляются в `.warnings`. Старых «модельных» заметок про
        вид в кеше быть не может: профиль сюда попадает только с
        `pattern_kinds_source != "model"`, то есть собранный вообще без
        `vision`."""
        with PptxPackage.open(path) as pkg:
            canvas = pkg.canvas()
            usage = collect_usage(pkg, canvas)
            type_scale = build_type_scale(pkg, canvas, usage)
            grid = build_grid(pkg, canvas)
            master_part = pick_primary_master(pkg)
            theme_for_layouts = read_theme(pkg, master_part)
            layouts = build_layout_catalog(
                pkg, canvas, theme_for_layouts, grid, usage=usage, type_scale=type_scale,
            )
            assets = build_asset_catalog(pkg, canvas, layouts)
            patterns = mine_patterns(pkg, canvas, grid, type_scale, assets)

        patterns, vision_notes = classify_patterns_by_vision(patterns, path, vision)

        # Превью — тот же рендер, что и уточнение видов выше (не
        # параллельно: этот метод — один дозапрос на кеш-хите, не полный
        # разбор `from_file`, лишний `ThreadPoolExecutor` ради одного
        # шага не по чем). Если что-то из старого кеша уже несло превью
        # (например, паттерн с уверенной геометрией, чей вид не менялся
        # этим дозапросом), а свежий рендер этого конкретного паттерна не
        # удался — старое превью не теряется, честный приоритет "свежее
        # лучше старого, старое лучше отсутствия".
        preview_dir = cache_dir / cached.fingerprint / "previews" if cache_dir is not None else None
        fresh_previews = _save_pattern_previews(patterns, path, preview_dir)
        old_previews = {p.pattern_id: p.preview_path for p in cached.patterns}

        provenance = [
            line for line in cached.provenance
            if not line.startswith(_VISION_PROVENANCE_PREFIXES)
        ]
        # `vision_notes` пуст только когда паттернов нет вовсе (см.
        # `classify_patterns_by_vision`) — тогда честнее оставить прежнюю
        # формулировку «моделью не уточнялся», чем выдумывать сводку.
        provenance.append(vision_notes[0] if vision_notes else _NO_VISION_PROVENANCE_LINE)

        return cached.model_copy(update={
            # Схема слотов, уже снятая моделью, переживает перемайнинг:
            # `pattern_id` и порядок слотов детерминированы и от вида
            # раскладки не зависят.
            "patterns": _apply_slot_schema([
                _pattern_model(p, fresh_previews.get(p.pattern_id) or old_previews.get(p.pattern_id))
                for p in patterns
            ], _slot_schema_of(cached.patterns)),
            "pattern_kinds_source": "model",
            "provenance": provenance,
            "warnings": list(cached.warnings) + list(vision_notes[1:]),
        })

    @classmethod
    def _describe_cached_slots(
        cls, cached: "TemplateProfile", path: Path, llm: VisionProvider, cache_dir: Path | None,
    ) -> "TemplateProfile":
        """Дозапрос схемы слотов поверх кеш-хита без неё — зеркало
        `_reclassify_pattern_kinds`. Перемайнивать не нужно: схеме хватает
        зеркал из кеша (роли, коробки, текст-образец, повтор). Картинки
        берутся из превью в кеше, недостающие рендерятся."""
        pngs: dict[str, bytes] = {}
        for model in cached.patterns:
            on_disk = cached.pattern_preview_path(model, cache_dir=cache_dir)
            if on_disk is not None and on_disk.exists():
                try:
                    pngs[model.pattern_id] = on_disk.read_bytes()
                except OSError:
                    pass
        missing = [m for m in cached.patterns if m.pattern_id not in pngs]
        if missing:
            pngs.update(_render_pattern_pngs(missing, path))
        if not pngs:
            return cached
        schema, notes = describe_pattern_slots(cached.patterns, pngs, llm)
        provenance = [
            line for line in cached.provenance if not line.startswith(_SCHEMA_PROVENANCE_PREFIX)
        ]
        return cached.model_copy(update={
            "patterns": _apply_slot_schema(cached.patterns, schema),
            "pattern_schema_source": "model",
            "provenance": provenance + notes[:1],
            "warnings": list(cached.warnings) + notes[1:],
        })

    def to_json(self) -> str:
        return self.model_dump_json()

    @property
    def canvas_norm(self) -> float:
        """Множитель денормировки типографической шкалы к РЕАЛЬНОМУ холсту
        этого шаблона (`Canvas.norm` — `12192000 / canvas_width_emu`,
        12192000 EMU = эталонный холст 13.333″, к которому `type_scale.
        steps` и родные кегли слотов паттернов приведены при сборке
        профиля). `1.0`, если `canvas_width_emu` не задан (защита от
        деления на ноль на синтетических профилях тестов, у которых холст
        не установлен)."""
        if not self.canvas_width_emu:
            return 1.0
        return Canvas(width_emu=self.canvas_width_emu, height_emu=self.canvas_height_emu).norm

    def denorm_pt(self, normalized_pt: float) -> float:
        """Денормирует кегль, приведённый к эталонному холсту 13.333″
        (`type_scale.steps`, `PatternSlot.size_pt`), к РЕАЛЬНОМУ кеглю
        холста этого шаблона — единственная арифметика, которая имеет
        право это делать (`normalized_pt / self.canvas_norm`).

        ЕДИНСТВЕННЫЙ путь положить кегль, взятый из нормированного
        источника, на слайд. Найдено аудитом (T02, отчёт задачи 10): на
        VK Tech (холст 10″, `canvas_norm` = 1.333) `compose/charts.py`,
        `tables.py` и `diagrams.py` читали `type_scale.steps` напрямую, в
        обход денормировки, которую `compose/builder.py` уже делал
        (`_shrink_sequence`) — кегли текста графиков/таблиц/схем выходили
        завышенными примерно на треть относительно остального слайда.
        Метод существует, чтобы такой обход было неоткуда взять: любой
        новый вызывающий код, которому нужен кегль ступени шкалы, идёт
        через `type_scale_pt`/`denorm_pt`, а не читает `type_scale.steps`
        напрямую."""
        return normalized_pt / self.canvas_norm

    def type_scale_pt(self, step: str, default: float | None = 0.0) -> float | None:
        """Кегль ступени `type_scale.steps` (`"micro"`/`"caption"`/
        `"body"`/`"h2"`/`"h1"`/`"display"`), денормированный к РЕАЛЬНОМУ
        холсту этого шаблона (см. `denorm_pt`). `default` — то же, что у
        `dict.get`, тоже денормируется (сам является кеглем шкалы по
        смыслу вызова); `default=None` возвращает `None` без деления,
        когда вызывающему коду важно ОТЛИЧИТЬ "ступени нет вовсе" от
        "кегль ступени равен нулю" (см. `compose/diagrams.py::_fit_label_
        size`, перебор ступеней по убыванию)."""
        raw = self.type_scale.steps.get(step, default)
        if raw is None:
            return None
        return self.denorm_pt(raw)

    def min_font_pt(self) -> float:
        """Нижняя граница кегля для любого ужимания текста: ступень caption
        шкалы ЭТОГО шаблона, денормированная к его холсту (раздел 14
        архитектуры). Раньше автопочинка держала глобальные 8pt, а сборка
        caption: у шаблона с caption 12pt починка опускала текст ниже
        того, что сборка сочла бы нечитаемым. Одна функция на обоих."""
        return self.type_scale_pt("caption", 12.0)

    def pattern_preview_path(
        self, pattern: PatternModel, *, cache_dir: Path | None = _CACHE_DIR_UNSET,  # type: ignore[assignment]
    ) -> Path | None:
        """Абсолютный путь к PNG-превью паттерна на диске, либо `None`, если
        превью не сохранялось (`pattern.preview_path is None`) или каталог
        кеша недоступен. `PatternModel.preview_path` сам по себе —
        ОТНОСИТЕЛЬНЫЙ путь (от `<cache_dir>/<fingerprint>/`, см. `_save_
        pattern_previews`), поэтому открыть файл, зная только его, нельзя
        без того же `cache_dir`, каким собирался профиль — этот метод
        разрешает разницу тем же приёмом, что `from_file` разрешает
        `cache_dir` не переданный (`_CACHE_DIR_UNSET` -> `_default_cache_
        dir()`) от переданного явно, включая `None` (кеш выключен -> превью
        негде искать)."""
        if pattern.preview_path is None:
            return None
        effective_cache_dir = cache_dir if cache_dir is not _CACHE_DIR_UNSET else _default_cache_dir()
        if effective_cache_dir is None:
            return None
        return Path(effective_cache_dir) / self.fingerprint / pattern.preview_path


# ---------------------------------------------------------------------------
# Отчёт «откуда что взято»
# ---------------------------------------------------------------------------

def _build_provenance(
    *, master_part: str, theme_part: str, theme: ThemeInfo, usage: Usage, type_scale: TypeScale,
    grid: Grid, assets: AssetCatalog, layouts: list[LayoutEntry], patterns: list[Pattern],
    palette_notes: list[PaletteNote], vision_notes: list[str] = (),
) -> list[str]:
    lines: list[str] = []

    lines.append(
        f"Тема бренда резолвится через relationship'ы: мастер `{master_part}` → тема "
        f"`{theme_part}` (не по имени файла — у шаблонов встречаются «сиротские» темы,"
        " ни к одному мастеру не привязанные)."
    )

    if theme.font_scheme_degraded:
        share = f"{theme.theme_font_share:.1%}" if theme.theme_font_share is not None else "неизвестно сколько"
        lines.append(
            f"Схема шрифтов темы (`a:fontScheme`, `{theme.scheme_name or 'Office'}`, "
            f"majorFont=minorFont=`{theme.major_font}`) — заглушка Google-экспорта и поэтому "
            f"проигнорирована как источник типографики (шрифт темы набрал лишь {share} "
            "фактического текста слайдов). Гарнитуры дизайн-системы определены по `a:latin` "
            "внутри run'ов на слайдах, а не по теме."
        )
    else:
        share = f"{theme.theme_font_share:.1%}" if theme.theme_font_share is not None else "большинство"
        lines.append(
            f"Гарнитура темы (`{theme.major_font}`) подтверждена фактическим текстом "
            f"слайдов ({share} символов набрано ею) и использована как основа шкалы."
        )

    lines.append(
        "Типографическая шкала (`" + ", ".join(f"{k}={v:g}pt" for k, v in sorted(type_scale.steps.items())) + "`) "
        "определена по кеглям плейсхолдеров лейаутов и фактической гистограмме кеглей run'ов на "
        "слайдах; там, где ни то ни другое не дало кегля (характерно для body-текста, если "
        "лейауты его вовсе не задают), кегль восстановлен модой по числу знаков среди всех "
        "run'ов шаблона — короткая подпись не должна перевешивать длинный абзац."
    )

    conf_items = sorted(grid.confidence.items())
    conf_text = ", ".join(f"{k}={v:.2f}" for k, v in conf_items)
    lines.append(
        f"Поля и колонки сетки восстановлены кластеризацией позиций плейсхолдеров и фигур по "
        f"слайдам и лейаутам (не заданы явно в формате — `p:sldLayout` не несёт полей страницы); "
        f"поле слева {grid.margin_left:.4f}, справа {grid.margin_right:.4f}, сверху "
        f"{grid.margin_top:.4f}, снизу {grid.margin_bottom:.4f} (доли холста); уверенность по "
        f"компонентам сетки: {conf_text}."
    )

    kinds_by_name = sum(1 for l in layouts if l.kind_confidence >= 0.6)
    lines.append(
        f"Тип каждого из {len(layouts)} лейаутов (`kind`) определён ансамблем имени файла лейаута "
        f"(словарь синонимов) и геометрической сигнатуры плейсхолдеров — имя одно без геометрии "
        f"часто врёт (декоративные шаблоны переименовывают лейауты при экспорте), геометрия одна "
        f"без имени не различает часть типов; согласны оба сигнала у {kinds_by_name} из {len(layouts)}."
    )

    if assets.logo is not None:
        lines.append(
            f"Логотип — `{assets.logo.part_name}`, выбран как маленький файл, на который ссылаются "
            f"макет/мастер, с шириной размещения в характерном для лого диапазоне холста и "
            f"максимальным числом таких размещений среди кандидатов (уверенность {assets.logo.confidence:.2f})."
        )
    else:
        lines.append(f"Логотип не определён: {assets.logo_reason or 'кандидатов не нашлось'}.")

    lines.append(
        f"Из {len(layouts)} лейаутов на слайдах-примерах намайнено {len(patterns)} композиционных "
        "паттернов (заголовок+контент, повторяющиеся карточки/списки, декоративные подложки) — "
        "каждый несёт список слайдов-источников, откуда он снят."
    )

    if palette_notes:
        lines.append("Роли палитры: " + "; ".join(n.text for n in palette_notes) + ".")

    if vision_notes:
        # Первая строка `vision_notes` — сводка ("N из M"), см.
        # `classify_patterns_by_vision`; она же единственная, что попадает в
        # provenance — отказы отдельных паттернов идут в warnings (см. вызов
        # `from_file` выше), провенанс не обязан перечислять каждый отказ.
        lines.append(vision_notes[0])
    else:
        lines.append(_NO_VISION_PROVENANCE_LINE)

    return lines


def _build_warnings(
    *, theme: ThemeInfo, usage: Usage, type_scale: TypeScale, grid: Grid,
    assets: AssetCatalog, layouts: list[LayoutEntry], palette_notes: list[PaletteNote],
) -> list[str]:
    warnings: list[str] = []

    if theme.text_styles_degraded:
        warnings.append(
            "Стили текста мастера (`p:txStyles`) — заглушка: все девять уровней titleStyle и "
            "bodyStyle несут одинаковую тройку (кегль/шрифт/цвет). Кегли и интерлиньяж взяты из "
            "фактического текста слайдов, а не из txStyles — доверять txStyles этого шаблона нельзя."
        )

    if theme.font_scheme_degraded:
        warnings.append(
            "Схема шрифтов темы (`a:fontScheme`) — заглушка Google-экспорта (Office/Arial=Arial) "
            "и проигнорирована при определении гарнитур дизайн-системы."
        )

    if theme.is_stock_office_palette:
        warnings.append(
            "Акцентные цвета темы совпадают со стоковой палитрой Office 2013+ — тема, скорее всего, "
            "не несёт брендовых цветов; палитра опирается на фактические цвета шейпов, а не на тему."
        )

    if usage.unresolved:
        reasons = {u.reason for u in usage.unresolved}
        warnings.append(
            f"{len(usage.unresolved)} цветовых элементов не резолвятся ({'; '.join(sorted(reasons))}) "
            "и не попали ни в одну палитру шаблона."
        )

    if usage.theme_fallbacks:
        warnings.append(
            f"{len(usage.theme_fallbacks)} част(и/ей) пакета получили тему первичного мастера вместо "
            "собственной (связь до их темы не резолвится) — палитра этих частей может быть неточной."
        )

    total_chars = usage.unstyled_chars + usage.explicit_style_chars
    if total_chars > 0:
        share = usage.unstyled_chars / total_chars
        if share >= _UNSTYLED_WARNING_SHARE:
            warnings.append(
                f"{share:.1%} текста набрано run'ами без единого явного свойства стиля (кегль/шрифт/"
                "цвет/bold/italic) — типографическая шкала опирается на меньшую часть текста, чем обычно."
            )

    low_conf_steps = {k: v for k, v in type_scale.step_confidence.items() if v < _LOW_CONFIDENCE_THRESHOLD}
    if low_conf_steps:
        items = ", ".join(f"{k}={v:.2f}" for k, v in sorted(low_conf_steps.items()))
        warnings.append(f"Низкая уверенность в ступенях типографической шкалы: {items}.")

    low_conf_grid = {k: v for k, v in grid.confidence.items() if v < _LOW_CONFIDENCE_THRESHOLD}
    if low_conf_grid:
        items = ", ".join(f"{k}={v:.2f}" for k, v in sorted(low_conf_grid.items()))
        warnings.append(f"Низкая уверенность в компонентах сетки: {items}.")

    low_conf_layouts = [l for l in layouts if l.kind_confidence < _LOW_CONFIDENCE_THRESHOLD]
    if low_conf_layouts:
        warnings.append(
            f"{len(low_conf_layouts)} из {len(layouts)} лейаутов классифицированы с уверенностью "
            f"ниже {_LOW_CONFIDENCE_THRESHOLD:.1f} — тип этих лейаутов (`kind`) стоит перепроверить вручную."
        )

    if assets.logo is None:
        warnings.append(f"Логотип не найден: {assets.logo_reason or 'кандидатов не нашлось'}.")

    if assets.unclassified:
        warnings.append(f"{len(assets.unclassified)} медиафайл(ов) не классифицированы ни в одну категорию ассетов.")

    if assets.boxless_placements:
        warnings.append(
            f"{assets.boxless_placements} размещени(й) картинок не удалось привязать к координатам "
            "(сломанная геометрия группы выше по дереву) — они не участвуют в классификации ассетов."
        )

    for note in palette_notes:
        if note.severity == "warning":
            warnings.append(f"Именование ролей палитры: {note.text}.")

    return warnings
