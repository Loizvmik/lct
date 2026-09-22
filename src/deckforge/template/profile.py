"""`TemplateProfile` — единый объект дизайн-системы, собранной из .pptx-шаблона.

Собирает воедино весь разбор предыдущих задач (тема, фактическая палитра и
шрифты, типографическая шкала, сетка, каталог лейаутов, каталог ассетов,
майнинг композиционных паттернов) плюс два требования этой задачи:

- именование ролей палитры моделью (`naming.py`, единственный модуль пакета,
  который реально вызывает модель — см. докстроку пакета; этот модуль
  импортирует из `deckforge.provider` только тип `LLMProvider` для сигнатуры
  параметра `namer` у `from_file` ниже, самого вызова здесь нет);
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
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from deckforge.ooxml.color import Color, UnresolvedColor
from deckforge.ooxml.package import PptxPackage
from deckforge.provider.base import LLMProvider
from deckforge.settings import Settings
from deckforge.template.assets import AssetCatalog, AssetRef, Placement, build_asset_catalog
from deckforge.template.grid import ColumnAxis, Grid, build_grid
from deckforge.template.layouts import Background, LayoutEntry, PlaceholderSlot, build_layout_catalog
from deckforge.template.naming import PaletteNote, name_palette_roles_report
from deckforge.template.patterns import Capacity, DecorShape, Pattern, PatternSlot, RepeatSpec, mine_patterns
from deckforge.template.theme import ThemeInfo, pick_primary_master, read_theme
from deckforge.template.typography import TypeScale, build_type_scale
from deckforge.template.usage import Usage, collect_usage

# config/app.yaml — единственная точка настройки, как и всё остальное в
# проекте (см. cli.py: тот же путь, тот же parents[N] от файла до корня
# репозитория — profile.py на один уровень глубже cli.py, отсюда [3], не [2]).
APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"

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
PROFILE_SCHEMA_VERSION = 1

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
        columns=[ColumnAxisModel(center=c.center, count=c.count, confidence=c.confidence) for c in grid.columns],
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


def _pattern_slot_model(slot: PatternSlot) -> PatternSlotModel:
    return PatternSlotModel(
        role=slot.role, box=_box_model(slot.box), size_pt=slot.size_pt, color_hex=slot.color_hex,
        align=slot.align, max_chars=slot.max_chars, wraps=slot.wraps, sample_text=slot.sample_text,
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


def _decor_shape_model(decor: DecorShape) -> DecorShapeModel:
    return DecorShapeModel(
        kind=decor.kind, box=_box_model(decor.box), rotation=decor.rotation,
        flip_h=decor.flip_h, flip_v=decor.flip_v, fill_hex=decor.fill_hex,
        has_fill=decor.has_fill, fill_kind=decor.fill_kind,
        repeat_group=decor.repeat_group, repeat_index=decor.repeat_index,
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


def _pattern_model(pattern: Pattern) -> PatternModel:
    return PatternModel(
        pattern_id=pattern.pattern_id, source_slide_index=list(pattern.source_slide_index),
        layout_id=pattern.layout_id, kind=pattern.kind,
        slots=[_pattern_slot_model(s) for s in pattern.slots],
        repeat=_repeat_spec_model(pattern.repeat),
        decor=[_decor_shape_model(d) for d in pattern.decor],
        capacity=_capacity_model(pattern.capacity), score=pattern.score, is_dark=pattern.is_dark,
    )


# ---------------------------------------------------------------------------
# TemplateProfile
# ---------------------------------------------------------------------------

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


class TemplateProfile(BaseModel):
    """Единый объект дизайн-системы шаблона — интерфейс брифа (Step 1)
    дословно (`.from_file`, `.to_json`, `.fingerprint`, `.provenance`,
    `.warnings`) плюс сами данные дизайн-системы, без которых генерации
    и аудиту, работающим по профилю (следующие задачи), нечем пользоваться."""

    source_name: str
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
    provenance: list[str]
    warnings: list[str]
    fingerprint: str
    schema_version: int = PROFILE_SCHEMA_VERSION

    @classmethod
    def from_file(
        cls, path: Path, *, namer: LLMProvider | None = None, cache_dir: Path | None = None,
    ) -> "TemplateProfile":
        """Разбирает `.pptx`-шаблон целиком, ровно один проход по пакету.

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
        (если он был бы нужен) сетевой вызов именования палитры не
        повторяются. Каталог кеша — `cache_dir`, если передан явно, иначе
        `paths.profile_cache` из `config/app.yaml` (см. `_default_cache_dir`
        ниже); если конфиг недоступен или каталог кеша не задан — кеш просто
        не используется, разбор идёт как обычно (кеш — оптимизация
        повторного вызова на одном и том же файле, а не обязательное
        условие сборки профиля). Повреждённый файл кеша не роняет вызов —
        профиль пересчитывается заново и перезаписывает его.

        Ключ кеша — только отпечаток ФАЙЛА, без учёта `namer` (Task 8
        код-ревью, находка 2). Раньше это значило: первый вызов на файле без
        ключа модели (namer=None, роли — запасным вариантом) навсегда
        отравлял кеш — следующий вызов с настоящим `namer` получал те же
        запасные роли, модель не вызывалась никогда. Починено: на кеш-хите,
        если сохранённый профиль несёт `palette_roles_source != "model"`
        (роли — запасной вариант, а не ответ модели) и сейчас передан
        `namer`, роли переназначаются моделью через
        `_reassign_palette_roles`, и кеш обновляется — остальной разбор
        (детерминированный, не изменился) не переделывается. Обратное
        (роли уже от модели, а вызов идёт без ключа) кеш отдаёт как есть —
        уже полученное от модели не деградирует до запасного варианта."""
        path = Path(path)
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()

        effective_cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()
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
                if namer is not None and cached.palette_roles_source != "model":
                    # Честная оговорка из докстроки выше: ключ кеша — только
                    # отпечаток файла, без учёта namer. Профиль мог лечь в
                    # кеш без ключа (роли — запасным вариантом) раньше, чем
                    # ключ появился. Раз модель сейчас доступна и роли ещё не
                    # от неё — переназначаем роли моделью и обновляем кеш, не
                    # трогая остальной (детерминированный, не изменившийся)
                    # разбор — см. `_reassign_palette_roles`.
                    cached = cls._reassign_palette_roles(cached, path, namer)
                    if cache_file is not None:
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

        # Тема для отчёта и именования палитры — уточнённая по фактическому
        # тексту слайдов (`Usage.primary_theme`, см. theme.
        # refine_font_scheme_degraded), не сырая `theme_for_layouts` выше:
        # каталог лейаутов исторически считается по сырой теме (см.
        # conftest.py), а отчёт человеку обязан отражать окончательный,
        # уточнённый вывод о деградации fontScheme.
        theme = usage.primary_theme or theme_for_layouts

        palette_report = name_palette_roles_report(usage, theme, namer)

        provenance = _build_provenance(
            master_part=master_part, theme_part=theme_part, theme=theme, usage=usage,
            type_scale=type_scale, grid=grid, assets=assets, layouts=layouts,
            patterns=patterns, palette_notes=palette_report.notes,
        )
        warnings = _build_warnings(
            theme=theme, usage=usage, type_scale=type_scale, grid=grid,
            assets=assets, layouts=layouts, palette_notes=palette_report.notes,
        )

        profile = cls(
            source_name=path.name,
            canvas_width_emu=canvas.width_emu, canvas_height_emu=canvas.height_emu,
            theme=_theme_model(theme), theme_part=theme_part, master_part=master_part,
            palette_roles=dict(palette_report.roles),
            palette_roles_source=palette_report.source,
            type_scale=_type_scale_model(type_scale), grid=_grid_model(grid),
            layouts=[_layout_entry_model(entry) for entry in layouts],
            assets=_asset_catalog_model(assets),
            patterns=[_pattern_model(p) for p in patterns],
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
            "provenance": provenance,
            "warnings": warnings,
        })

    def to_json(self) -> str:
        return self.model_dump_json()


# ---------------------------------------------------------------------------
# Отчёт «откуда что взято»
# ---------------------------------------------------------------------------

def _build_provenance(
    *, master_part: str, theme_part: str, theme: ThemeInfo, usage: Usage, type_scale: TypeScale,
    grid: Grid, assets: AssetCatalog, layouts: list[LayoutEntry], patterns: list[Pattern],
    palette_notes: list[PaletteNote],
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
