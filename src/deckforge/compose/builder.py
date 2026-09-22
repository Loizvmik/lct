"""Сборка `.pptx` — колода заводится ОТ САМОГО ФАЙЛА ШАБЛОНА
(`Presentation(template_path)`, не `Presentation()` с нуля): мастера,
лейауты, тема, встроенные шрифты и media остаются нативными, слайды-
примеры удаляются аккуратно (`p:sldIdLst` + relationships, см.
`_clear_sample_slides`) — так проверка аудита T04 «слайд собран не на
макете из шаблона» проходит по построению, а не по совпадению.

Единственное место в `compose/`, которое трогает и `textfit` (замер), и
python-pptx (рисование) одновременно — блоки контента (`compose.blocks`)
и декор (`compose.decor`) сами python-pptx не касаются.
"""
from __future__ import annotations
import io
import re
from dataclasses import dataclass, replace
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from deckforge.compose.blocks import Paragraph, SlotContent, assign_content, expand_decor, find_bullet_char
from deckforge.compose.charts import ChartSpec, Series, add_chart
from deckforge.compose.decor import apply_decor
from deckforge.compose.tables import TableSpec, add_table
from deckforge.compose.textfit import measure, register_template_fonts
from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.plan.spec import BulletBlock, CardBlock, DeckSpec, KpiBlock, QuoteBlock, SlideSpec, TextBlock
from deckforge.plan.variants import Variant
from deckforge.settings import Settings
from deckforge.template.grid import ColumnAxis, Grid
from deckforge.template.naming import MIN_CONTRAST
from deckforge.template.patterns import Capacity, DecorShape, Pattern, PatternSlot, RepeatSpec
from deckforge.template.profile import LayoutEntryModel, TemplateProfile

APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"
EMU_PER_INCH = 914400

# `Variant` теперь определён один раз в `plan.variants` (Task 13) — `compose`
# уже зависит от `plan` (импортирует `plan.spec` для типов содержания), тот
# же однонаправленный порядок зависимостей, только для варианта вёрстки, а
# не для схемы данных. Реэкспорт (`from ... import Variant` выше) сохраняет
# `deckforge.compose.builder.Variant` рабочим для существующего кода/тестов,
# которые импортировали его именно отсюда (Task 9-10, до этой задачи) —
# `Variant.dense`/`Variant.visual` те же самые объекты, что и в `plan.
# variants.Variant`, `Variant.airy` — новый третий вариант оттуда же.


@dataclass(frozen=True)
class Fit:
    """Лезет ли содержание слайда в раскладку паттерна — используется на
    выборе паттерна (см. `_pick_pattern`) и остаётся доступным вызывающему
    коду (Task 10a: "ни одна не подходит — слайд уходит в песочницу").

    Правка по итогам визуального ревью (отчёт задачи, находка №1): раньше
    `overflow_ratio` считался на СОБСТВЕННОМ (native) кегле паттерна, без
    ужимания — наивная проверка отбраковывала раскладки, которые
    `_draw_slot` потом УСПЕШНО укладывал, ужав шрифт по шкале `TypeScale`.
    Теперь `overflow_ratio` — лучший (наименьший) результат по ВСЕЙ шкале
    ужимания вплоть до `caption` (то же самое, что реально попробует
    `_draw_slot`), 0.0 — есть кегль шкалы, на котором слот не переполнен
    нигде. `reason` — какой слот и почему (плюс `"exact"`/`"fallback"` про
    шрифт замера, см. `font_source`, — пригодится отчёту/аудиту, чтобы
    отличать надёжный overflow от посчитанного с запасом на неточный
    шрифт).

    `fill_ratio` — доля площади холста, которую реально займёт содержание
    в этой раскладке (сумма площадей слотов, в которые попал контент, как
    доля холста 0..1) — нужен подборщику, чтобы не брать формально
    влезающую, но почти пустую раскладку (ТЗ D05: "слайд заполнен меньше
    четверти или больше трёх четвертей — брак"; пороги `_FILL_RATIO_MIN`/
    `_FILL_RATIO_MAX` те же 0.25/0.75, что и в плане будущего
    `config/audit.yaml` — см. `docs/superpowers/plans/2026-09-21-
    deckforge.md`, ещё не заведён отдельным конфигом в границах этой
    задачи)."""

    ok: bool
    overflow_ratio: float
    reason: str
    fill_ratio: float = 0.0
    font_source: str = "exact"


class BuildError(RuntimeError):
    """Структурная невозможность собрать слайд — лейаут паттерна пропал из
    шаблона (не должно случаться на профиле, разобранном с того же файла,
    но честная ошибка лучше тихого `None`)."""


_ALIGN_MAP = {"l": PP_ALIGN.LEFT, "ctr": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT, "just": PP_ALIGN.JUSTIFY}

# Допуск "влезает" — доля дюйма, компенсирующая округление EMU↔дюйм и
# integer-округление кегля в пикселях при замере (`textfit.measure` округляет
# `size_pt` до целого пикселя) — то же значение, что у контракта T04-теста
# брифа (`+0.05`), но взято с запасом впятеро меньше него: наш собственный
# порог "влезает" обязан быть строже потребительской проверки (тот же
# принцип, что `patterns._within_margins` строже `test_slots_respect_
# template_margins` — иначе плавающая погрешность может протащить то, что
# здесь прошло, но не пройдёт там).
_FIT_TOLERANCE_IN = 0.01

# Ступени шкалы, по которым ужимается текст — БЕЗ "micro" (брифом: "не ниже
# подписи", т.е. "caption" — жёсткий пол).
_SHRINK_STEPS = ("display", "h1", "h2", "body", "caption")

# Роли, для которых используется ЗАГОЛОВОЧНЫЙ интерлиньяж/начертание
# шаблона, а не текстовый — тот же список смысла, что `TITLE_PH_TYPES`
# в template/grid.py, только по ролям слота, не по типу плейсхолдера
# (`PatternSlot` не несёт `ph_type`).
_HEADING_ROLES = frozenset({"headline", "subhead", "quote", "card_title", "kpi_value"})

_ROLE_COLOR = {
    "headline": "on_surface", "subhead": "muted", "body": "on_surface", "bullets": "on_surface",
    "card_title": "on_surface", "card_body": "on_surface", "kpi_value": "brand", "kpi_label": "muted",
    "quote": "on_surface", "quote_author": "muted", "source": "muted", "caption": "muted",
}


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------


def build_deck(spec: DeckSpec, profile: TemplateProfile, template_path: Path, variant: Variant) -> Path:
    register_template_fonts(template_path)
    with PptxPackage.open(template_path) as pkg:
        bullet_char = find_bullet_char(pkg)

    prs = Presentation(str(template_path))
    _clear_sample_slides(prs)

    patterns = [_pattern_from_model(m) for m in profile.patterns]
    for slide_spec in spec.slides:
        pattern = _resolve_pattern(slide_spec, patterns, profile, variant)
        if pattern is None:
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: для kind={slide_spec.kind!r} не нашлось ни одного "
                "паттерна этого шаблона — слайд не собран."
            )
            continue
        fit = fits(slide_spec, pattern, profile)
        if not fit.ok:
            # Бриф: "если ни одна раскладка не подходит, выбирай ту, где
            # переполнение наименьшее, и оставляй явный след о проблеме" —
            # след пишется здесь, в момент выбора, а не только если потом
            # дойдёт до усечения в `_draw_slot` (сам выбор уже "наименее
            # плохой" среди кандидатов этого kind, это надо знать заранее).
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: содержание не помещается ни в одну раскладку вида "
                f"{slide_spec.kind!r} даже на минимальном кегле — выбрана раскладка с наименьшим "
                f"переполнением ({fit.overflow_ratio:.0%}, {fit.reason})."
            )
        place_slide(prs, slide_spec, pattern, profile, bullet_char=bullet_char)

    out_path = _output_path(spec, template_path, variant)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path


def place_slide(
    prs, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, *, bullet_char: str = "•",
) -> None:
    layout = _find_layout(prs, pattern.layout_id)
    if layout is None:
        raise BuildError(f"лейаут {pattern.layout_id!r} не найден в открытом шаблоне")
    slide = prs.slides.add_slide(layout)

    canvas_width_emu = profile.canvas_width_emu
    canvas_height_emu = profile.canvas_height_emu
    grid = _grid_from_model(profile.grid)

    # Декор группы повтора (плашки карточек и т.п.) разворачивается под
    # фактическое число элементов ВМЕСТЕ с текстовыми слотами — правка по
    # итогам повторного визуального ревью (отчёт задачи, находка №1,
    # "главная находка"): раньше `pattern.decor` переносился статически,
    # независимо от того, сколько карточек реально легло на слайд, и
    # раскладка на шесть карточек под два элемента содержания оставляла
    # четыре пустые рамки. Декор вне группы повтора (`expand_decor` не
    # трогает `repeat_group=False`) переносится как и раньше.
    decor = expand_decor(pattern, _repeat_item_count(slide_spec), grid)
    apply_decor(slide, decor, canvas_width_emu, canvas_height_emu)
    # `_local_background_is_dark` ищет охватывающую плашку декора ПОД
    # слотом (см. её докстроку) — обязана видеть УЖЕ развёрнутые позиции
    # плашек (`decor`, не статический `pattern.decor`), иначе контраст
    # карточки №2 может посчитаться от плашки, стоявшей там на
    # исходном, ненамайненном слайде-примере.
    effective_pattern = pattern if decor is pattern.decor else replace(pattern, decor=decor)

    # Источник истины для фона под слотом — МАКЕТ, на который слайд реально
    # ставится (`profile.layouts`, разобран надёжно: наследование от
    # мастера, градиенты, фоновые фото — Task 6/8), а НЕ `pattern.is_dark`
    # (Task 10 отчёт, находка №1: раскладка, снятая со светлого
    # слайда-примера, но положенная на тёмный макет, несла `is_dark=False`
    # от примера — чёрный текст ложился на тёмно-фиолетовый фон макета,
    # нечитаемо). `layout_bg_luminance` — `None`, если макет пропал из
    # каталога (не должно случаться на профиле, разобранном с того же
    # файла) или у него самого не резолвился фон — тогда
    # `_local_background_luminance` честно падает на `pattern.is_dark` как
    # на последний осмысленный сигнал.
    layout_entry = _layout_entry(profile, pattern.layout_id)
    layout_bg_luminance = layout_entry.background.luminance if layout_entry is not None else None

    family = _primary_family(profile)
    for content in assign_content(slide_spec, pattern, grid):
        # Наложение — не занято ли место декором раскладки (Task 10 отчёт,
        # находка №2: заголовок налез на плашку декора) — проверяется и
        # чинится (сдвигом) ДО замера/отрисовки, слот замеряется уже по
        # исправленному боксу.
        box = _avoid_decor_overlap(content.slot.box, effective_pattern.decor, grid)
        if box is not content.slot.box:
            content = replace(content, slot=replace(content.slot, box=box))
        # Контраст — от фона НЕПОСРЕДСТВЕННО под этим слотом (плашка декора,
        # если слот на ней стоит, иначе фон макета) — см.
        # `_local_background_luminance`.
        _draw_slot(
            slide, slide_spec, content, profile, family, bullet_char, canvas_width_emu, canvas_height_emu,
            _local_background_luminance(content.slot.box, effective_pattern, layout_bg_luminance),
        )

    _place_visual(slide, slide_spec, pattern, profile)
    _remove_empty_placeholders(slide)


# ---------------------------------------------------------------------------
# Плейсхолдеры макета, унаследованные слайдом (Task 13, критичный дефект
# отчёта задачи, находка №1) — `prs.slides.add_slide(layout)` копирует на
# слайд ВСЕ плейсхолдеры лейаута (python-pptx, `SlideShapes.clone_layout_
# placeholders`), а этот модуль ни разу не пишет содержание НЕПОСРЕДСТВЕННО
# в плейсхолдер: `_draw_slot` всегда добавляет отдельный `add_textbox` по
# координатам слота (см. её комментарий про `tf.margin_*`), `_place_
# picture_visual`/`compose.charts.add_chart`/`compose.tables.add_table` —
# отдельные фигуры поверх. Незаполненный плейсхолдер в LibreOffice рисуется
# пустотой, в PowerPoint — видимой надписью "Щелкните, чтобы добавить
# текст"/"чтобы добавить рисунок" — на защите это видно на каждом слайде
# богатого макета (живой разбор ЛЦТ2026: слайд 4 макета "Содержание_1",
# девятнадцать пустых плейсхолдеров плюс четыре пустые карточные плашки
# декора — найдено координатором на живом рендере собранной колоды).
# ---------------------------------------------------------------------------

# Плейсхолдеры, которые PowerPoint заполняет САМ во время показа (номер
# слайда, дата, нижний колонтитул, шапка) — пустые в разметке файла, но
# осмысленные, брифом задачи явно требует их не трогать. python-pptx
# `clone_layout_placeholders` их и так не клонирует на слайд вовсе (её
# докстрока: "Latent placeholders (date, slide number, and footer) are not
# cloned") — проверка по типу здесь защитная, на случай будущей версии
# библиотеки или лейаута, который несёт такой плейсхолдер не-латентно.
_AUTO_FILLED_PLACEHOLDER_TYPES = frozenset({
    PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.HEADER,
})


def _placeholder_is_empty(shape) -> bool:
    """Плейсхолдер не несёт ни текста, ни картинки. Этот модуль никогда не
    пишет НИ ТО, НИ ДРУГОЕ прямо в унаследованный от лейаута плейсхолдер
    (см. докстроку раздела) — проверка на текст/картинку, а не безусловное
    удаление, оставлена честно: если однажды появится путь, кладущий
    контент прямо в плейсхолдер, эта функция не снесёт его."""
    if shape.has_text_frame and shape.text_frame.text.strip():
        return False
    if shape._element.findall(".//" + qn("a:blip")):
        return False
    return True


def _remove_empty_placeholders(slide) -> None:
    """Убирает из XML слайда плейсхолдеры лейаута, оставшиеся незаполненными
    после укладки содержания (см. докстроку раздела выше) — вызывается В
    КОНЦЕ `place_slide`, когда весь текст/визуал этого слайда уже
    отрисован, чтобы не удалить плейсхолдер раньше, чем стало известно,
    что он не понадобился."""
    for shape in list(slide.placeholders):
        if shape.placeholder_format.type in _AUTO_FILLED_PLACEHOLDER_TYPES:
            continue
        if _placeholder_is_empty(shape):
            shape._element.getparent().remove(shape._element)


# ---------------------------------------------------------------------------
# Визуал слайда (Task 13) — таблица/график/фото/иконка. `plan.spec.Visual`
# несёт только СОДЕРЖАНИЕ (числа таблицы, ряды графика, "какой ассет по
# смыслу"), КАК это лечь на холст — решает этот модуль, тем же принципом,
# что и текстовые слоты (`_draw_slot`): единственное место, трогающее и
# `TemplateProfile`, и python-pptx одновременно для визуала, — здесь, не в
# `plan/` и не в `compose.charts`/`compose.tables` (те двое уже готовы,
# Task 9-10, и НЕ знают о `plan.spec.Visual` вовсе — принимают свои
# собственные `ChartSpec`/`TableSpec`, этот модуль их строит).
# ---------------------------------------------------------------------------


def _visual_slot(pattern: Pattern, role: str) -> PatternSlot | None:
    """Самый ёмкий (по площади) слот `pattern` этой роли — тот же приём,
    что `blocks._slots_by_role` использует для текстовых слотов (см. её
    докстроку про Task 9 повторное ревью, находка №2): визуал кладётся в
    слот, который реально вмещает контент, а не в первый попавшийся."""
    candidates = [s for s in pattern.slots if s.role == role]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.box.width * s.box.height)


def _place_visual(slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile) -> None:
    visual = slide_spec.visual
    if visual is None:
        return

    if visual.kind == "table" and visual.table is not None:
        _place_table_visual(slide, slide_spec, pattern, profile, visual.table)
    elif visual.kind == "chart" and visual.chart is not None:
        _place_chart_visual(slide, slide_spec, pattern, profile, visual.chart)
    elif visual.kind in ("photo", "icon"):
        _place_picture_visual(slide, slide_spec, pattern, profile, visual.kind)


def _place_table_visual(slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, table) -> None:
    slot = _visual_slot(pattern, "table")
    if slot is None:
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: в раскладке {pattern.pattern_id!r} нет слота под таблицу "
            "— TableVisual не отрисован."
        )
        return
    if not table.rows:
        return

    header, body_rows = table.rows[0], table.rows[1:]
    cap = pattern.capacity
    truncated = False
    if cap.max_cols and len(header) > cap.max_cols:
        header = header[: cap.max_cols]
        body_rows = [row[: cap.max_cols] for row in body_rows]
        truncated = True
    max_body_rows = max(cap.max_rows - 1, 1) if cap.max_rows else len(body_rows)
    if len(body_rows) > max_body_rows:
        body_rows = body_rows[:max_body_rows]
        truncated = True
    if truncated:
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: таблица усечена до вместимости раскладки "
            f"({pattern.pattern_id!r}, max_rows={cap.max_rows}, max_cols={cap.max_cols})."
        )

    align = ["r" if all(_looks_numeric(c) for c in [h] + [r[i] for r in body_rows if i < len(r)])
             else "l" for i, h in enumerate(header)]
    add_table(slide, slot.box, TableSpec(header=header, rows=body_rows, align=align), profile)


_NUMERIC_CELL_RE = re.compile(r"^[+-]?[\d\s.,%]+[a-zа-яё%]*$", re.IGNORECASE)


def _looks_numeric(cell: str) -> bool:
    return bool(_NUMERIC_CELL_RE.match(cell.strip())) if cell and cell.strip() else False


def _place_chart_visual(slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, chart) -> None:
    slot = _visual_slot(pattern, "chart") or _visual_slot(pattern, "table") or _visual_slot(pattern, "image")
    if slot is None:
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: в раскладке {pattern.pattern_id!r} нет слота под график "
            "— ChartVisual не отрисован."
        )
        return
    if not chart.series:
        return

    cap = pattern.capacity
    series = chart.series
    if cap.max_series and len(series) > cap.max_series:
        series = series[: cap.max_series]
        slide_spec.findings.append(
            f"Слайд {slide_spec.index}: график усечён до {cap.max_series} рядов по вместимости "
            f"раскладки {pattern.pattern_id!r}."
        )

    # I05 (аудит): подписи осей/единиц обязательны для немаркерных типов —
    # чем бы ни ответила модель (или её не было вовсе), ось не должна
    # остаться безымянной, тот же принцип "код не доверяет слепо", что и у
    # `_color_for_role`/`_best_contrast_color` выше в этом файле.
    axis_titles = chart.axis_titles
    if not axis_titles or not axis_titles[0] or not axis_titles[1]:
        axis_titles = ("Категория", chart.unit or "Значение")

    spec = ChartSpec(
        kind=chart.kind, categories=list(chart.categories),
        series=[Series(name=s.name, values=list(s.values)) for s in series],
        unit=chart.unit, highlight_index=chart.highlight_index, axis_titles=axis_titles,
    )
    try:
        add_chart(slide, slot.box, spec, profile)
    except ValueError as exc:
        slide_spec.findings.append(f"Слайд {slide_spec.index}: график не построен ({exc}).")


def _place_picture_visual(slide, slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile, kind: str) -> None:
    slot = (
        _visual_slot(pattern, "image") if kind == "photo" else _visual_slot(pattern, "icon")
    ) or _visual_slot(pattern, "image") or _visual_slot(pattern, "icon")
    if slot is None:
        return  # раскладка не несёт визуального слота вовсе — нечего заполнять, не находка

    catalog = list(profile.assets.photos if kind == "photo" else profile.assets.icons)
    if not catalog:
        catalog = list(profile.assets.photos) + list(profile.assets.icons)
    if not catalog or not profile.source_path:
        return  # у шаблона нет своих фото/иконок (или профиль без source_path) — честно ничего не подставляем

    asset = max(catalog, key=lambda a: a.confidence)
    left, top, width, height = _emu_visual_box(slot.box, profile)

    try:
        with PptxPackage.open(Path(profile.source_path)) as pkg:
            data = pkg.part(asset.part_name)
    except Exception:
        return

    pic_left, pic_top, pic_width, pic_height = left, top, width, height
    if asset.width and asset.height:
        # "Contain", не растяжение на весь слот (L07 аудита следит именно
        # за отклонением placed_aspect/native_aspect) — картинка вписывается
        # в слот целиком по большей стороне и центрируется по меньшей.
        native_aspect = asset.width / asset.height
        slot_aspect = width / height if height else native_aspect
        if native_aspect > slot_aspect:
            pic_width = width
            pic_height = round(width / native_aspect)
            pic_top = top + (height - pic_height) // 2
        else:
            pic_height = height
            pic_width = round(height * native_aspect)
            pic_left = left + (width - pic_width) // 2

    slide.shapes.add_picture(
        io.BytesIO(data), Emu(pic_left), Emu(pic_top), Emu(max(1, pic_width)), Emu(max(1, pic_height)),
    )


def _emu_visual_box(box: Box, profile: TemplateProfile) -> tuple[int, int, int, int]:
    return (
        round(box.left * profile.canvas_width_emu), round(box.top * profile.canvas_height_emu),
        round(box.width * profile.canvas_width_emu), round(box.height * profile.canvas_height_emu),
    )


def fits(slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile) -> Fit:
    """Лезет ли содержание `slide_spec` в `pattern` — более лёгкая проверка,
    чем реальная укладка (`place_slide`), для выбора паттерна ДО того, как
    тратить время на построение слайда.

    Шкала ужимания та же, что реально попробует `_draw_slot`
    (`_shrink_sequence`) — иначе (см. докстроку `Fit`) эта проверка
    отбраковывала бы раскладки, которые сборка успешно укладывает ужатым
    шрифтом."""
    grid = _grid_from_model(profile.grid)
    assignments = assign_content(slide_spec, pattern, grid)
    canvas_width_in = profile.canvas_width_emu / EMU_PER_INCH
    canvas_height_in = profile.canvas_height_emu / EMU_PER_INCH
    family = _primary_family(profile)

    worst_ratio = 0.0
    worst_reason = ""
    font_source = "exact"
    canvas_area_in2 = canvas_width_in * canvas_height_in
    covered_area = 0.0
    for content in assignments:
        text = _joined_text(content.paragraphs)
        if not text.strip():
            continue
        box_width_in = content.slot.box.width * canvas_width_in
        box_height_in = content.slot.box.height * canvas_height_in
        if box_height_in <= 0:
            continue
        line_spacing = _line_spacing_for(content.role_hint, profile)
        slot_ratio = None
        best_height_in = box_height_in
        for size_pt in _shrink_sequence(profile, content.slot.size_pt):
            metrics = measure(text, family, size_pt, box_width_in, line_spacing=line_spacing)
            if metrics.font_source == "fallback":
                font_source = "fallback"
            ratio = metrics.height_in / box_height_in - 1.0
            if slot_ratio is None or ratio < slot_ratio:
                slot_ratio, best_height_in = ratio, metrics.height_in
            if slot_ratio <= 0.0:
                break  # нашли кегль шкалы, на котором слот не переполнен — дальше мельчить незачем
        slot_ratio = max(slot_ratio or 0.0, 0.0)
        if slot_ratio > worst_ratio:
            worst_ratio = slot_ratio
            worst_reason = (
                f"слот «{content.role_hint}» переполнен на {slot_ratio:.0%} даже на минимальном "
                "кегле шкалы (caption)"
            )
        # Доля холста, реально занятая ЧЕРНИЛАМИ этого слота — по факту
        # нарисованного текста (высота на выбранном кегле, отсечённая рамкой
        # слота), НЕ по площади самого слота. Находка визуального ревью
        # (отчёт задачи, №3, "слайд заполнен меньше четверти"): слот
        # `bullets`/`body` часто высокий по замыслу раскладки, но текст
        # начинается сверху и не растягивается на всю высоту (`_draw_slot`
        # не центрирует и не растягивает по вертикали) — если считать по
        # площади СЛОТА, а не по факту нарисованного текста, подборщик не
        # видит, что три коротких буллета оставляют низ слайда пустым.
        ink_height_in = min(best_height_in, box_height_in)
        if canvas_area_in2 > 0:
            covered_area += (ink_height_in * box_width_in) / canvas_area_in2

    missing = _missing_signals(slide_spec, assignments)
    if missing:
        worst_ratio = max(worst_ratio, 1.0)
        worst_reason = f"нет слота под роль(и): {', '.join(missing)}"

    ok = worst_ratio <= 0.0
    return Fit(
        ok=ok, overflow_ratio=max(worst_ratio, 0.0), reason=worst_reason or "содержание помещается",
        fill_ratio=covered_area, font_source=font_source,
    )


# Пороги "не слишком пусто / не слишком плотно" — те же числа, что ТЗ
# отводит будущей проверке аудита D05 (`fill_ratio_min`/`fill_ratio_max`,
# см. докстроку `Fit`) — подбор паттерна здесь пользуется теми же порогами,
# чтобы не выбирать раскладку, которую аудит потом всё равно забракует.
_FILL_RATIO_MIN = 0.25
_FILL_RATIO_MAX = 0.75


def _repeat_item_count(slide_spec: SlideSpec) -> int | None:
    """Число элементов, которые РЕАЛЬНО развернут `pattern.repeat` на этом
    слайде — сегодня это только `CardBlock.items` (единственный блок,
    зовущий `expand_repeat`/`expand_decor`, см. `_assign_cards` в
    `blocks.py`; `KpiBlock` раздаёт KPI-слоты напрямую, без пересчёта
    геометрии повтора). `None`, если на слайде нет такого блока — ни
    подбору паттерна (`_capacity_badness`), ни развороту декора
    (`place_slide`) не с чем сверять вместимость раскладки."""
    for block in slide_spec.blocks:
        if isinstance(block, CardBlock) and block.items:
            return len(block.items)
    return None


# Вес расхождения вместимости раскладки при ранжировании кандидатов —
# см. `_capacity_badness`: доля (не абсолютное число) намайненной
# `Capacity.max_items`, на которую она разошлась с фактическим объёмом
# содержания. Обоснование доли, а не абсолютной разницы (постановщик,
# "порог обоснуй долей, а не наблюдением за файлами"): сама ёмкость
# раскладки — величина шаблон-специфичная (3 карточки у одного шаблона,
# 8 у другого), абсолютная "на 4 карточки больше" ничего не говорит без
# знания масштаба самой раскладки, а доля от max_items сразу отвечает на
# вопрос "во сколько раз раскладка избыточна/недостаточна" одинаково на
# любом шаблоне: раскладка на 6 под 2 элемента даёт 4/6 ≈ 0.67 — почти
# такая же избыточность, что и раскладка на 3 под 1 элемент (2/3 ≈ 0.67),
# хотя абсолютная разница у них разная (4 против 2).
def _capacity_badness(pattern: Pattern, slide_spec: SlideSpec) -> float:
    """0.0, когда `Capacity.max_items` раскладки совпадает с фактическим
    числом элементов содержания слайда (или когда сравнивать не с чем —
    не card-слайд, см. `_repeat_item_count`), иначе — относительная доля
    расхождения. Используется только как ранжирующий сигнал `_pick_pattern`
    (не жёсткий отказ: раскладка с "неидеальной" вместимостью всё ещё
    может быть единственным кандидатом вида `kind` — лучше неидеальный
    выбор, чем никакого, тот же принцип, что и `_fill_badness`).

    Находка ревью ("главная находка"): раскладка, рассчитанная на шесть
    элементов, под два элемента содержания — плохой выбор, ДАЖЕ ЕСЛИ текст
    формально влезает (`fit.ok`) и не выглядит пустым по `fill_ratio`
    (`fill_ratio` меряет ЧЕРНИЛА фактически положенного текста, а не число
    пустых декоративных рамок вокруг него — это разные сигналы, `Capacity.
    max_items` нужен независимо)."""
    n = _repeat_item_count(slide_spec)
    if n is None or pattern.capacity.max_items <= 0:
        return 0.0
    return abs(pattern.capacity.max_items - n) / pattern.capacity.max_items


def _fill_badness(fill_ratio: float) -> float:
    """0.0, когда `fill_ratio` укладывается в [_FILL_RATIO_MIN,
    _FILL_RATIO_MAX], иначе — на сколько (в долях холста) он вышел за
    границу; используется только как ранжирующий сигнал `_pick_pattern`
    (не как жёсткий отказ — жёсткий отказ "слишком пусто/плотно" по всей
    колоде целиком, не по одному кандидату, работа будущего аудита D05)."""
    if fill_ratio < _FILL_RATIO_MIN:
        return _FILL_RATIO_MIN - fill_ratio
    if fill_ratio > _FILL_RATIO_MAX:
        return fill_ratio - _FILL_RATIO_MAX
    return 0.0


# ---------------------------------------------------------------------------
# Подбор паттерна (заглушка Task 13 — см. докстроку Variant)
# ---------------------------------------------------------------------------


def _missing_signals(slide_spec: SlideSpec, assignments: list[SlotContent]) -> list[str]:
    """Роли контента, для которых В ЭТОЙ раскладке не нашлось слота —
    `assign_content` молча пропускает контент без подходящей роли (см. её
    докстроку), а для выбора паттерна (`_pick_pattern`) это должно СЧИТАТЬСЯ
    переполнением: раскладка без слота под заголовок карточки не "лучше
    заполнена", чем раскладка, где заголовок карточки просто некуда
    положить (найдено тестом `test_cards_expand_to_the_actual_number_of_
    items` — без этой проверки `_pick_pattern` выбирал первый попавшийся
    "cards"-паттерн шаблона, даже если у него не было слота card_title, и
    заголовки карточек молча терялись)."""
    covered = {a.role_hint for a in assignments}
    missing: list[str] = []
    if slide_spec.headline and "headline" not in covered:
        missing.append("headline")
    for block in slide_spec.blocks:
        if isinstance(block, TextBlock) and "body" not in covered:
            missing.append("body")
        elif isinstance(block, BulletBlock) and "bullets" not in covered:
            missing.append("bullets")
        elif isinstance(block, QuoteBlock) and "quote" not in covered:
            missing.append("quote")
        elif isinstance(block, CardBlock):
            if block.items and "card_body" not in covered:
                missing.append("cards")
            if any(c.title for c in block.items) and "card_title" not in covered:
                missing.append("card_title")
        elif isinstance(block, KpiBlock):
            if block.items and "kpi_value" not in covered:
                missing.append("kpi_value")
            if any(k.label for k in block.items) and "kpi_label" not in covered:
                missing.append("kpi_label")
    return missing


def _pick_pattern(
    slide_spec: SlideSpec, patterns: list[Pattern], profile: TemplateProfile, variant: Variant,
) -> Pattern | None:
    """Перебирает кандидатов `pattern.kind == slide_spec.kind` и берёт того,
    в кого содержание влезает (`fits()`, шкала ужимания целиком, не только
    свой кегль) — не первого попавшегося. Порядок ранжирования:

    1. влезает ли вообще (`fit.ok`) — не влезающие кандидаты хуже любого
       влезающего;
    2. среди не влезающих — наименьшее `overflow_ratio` (бриф: "если ни
       одна не подходит, выбирай ту, где переполнение наименьшее");
    3. вместимость раскладки (`_capacity_badness`, находка повторного
       визуального ревью, "главная находка") — раскладка, чья `Capacity.
       max_items` заметно расходится с фактическим числом элементов
       содержания (шесть слотов повтора под два элемента), хуже раскладки
       той же вместимости, ДАЖЕ КОГДА содержание формально влезает и не
       выглядит пустым по `fill_ratio` — иначе выбор физически нечем
       отличить раскладку, оставляющую четыре пустых декоративных рамки, от
       нормально заполненной;
    4. "не слишком пусто/плотно" (`_fill_badness`, ТЗ D05) — среди влезающих
       кандидатов раскладка, где содержание не тонет в пустоте и не
       перегружает холст, предпочтительнее формально влезающей, но
       занимающей четверть холста;
    5. паттерн с более высоким `score` (майнинг увереннее в нём);
    6. `Variant.visual`/`Variant.dense` — тот же бонус/штраф за декор, что и
       раньше (временная эвристика Task 13, см. докстроку `Variant`)."""
    candidates = [p for p in patterns if p.kind == slide_spec.kind]
    if not candidates:
        return None

    def rank(p: Pattern):
        fit = fits(slide_spec, p, profile)
        # `airy` не смещает выбор по декору вовсе (0) — её плотность решает
        # `kind`, уже проставленный `plan.variants.apply_variant` ДО того,
        # как сюда дошёл вызов (см. докстроку `Variant` и `_resolve_pattern`
        # ниже: при заданном `pattern_id` этот ранжир вообще не вызывается,
        # он остаётся запасным путём для `pattern_id=None`).
        if variant is Variant.visual:
            visual_bias = len(p.decor)
        elif variant is Variant.dense:
            visual_bias = -len(p.decor)
        else:
            visual_bias = 0
        return (
            0 if fit.ok else 1, fit.overflow_ratio, _capacity_badness(p, slide_spec),
            _fill_badness(fit.fill_ratio), -p.score, -visual_bias,
        )

    return min(candidates, key=rank)


def _resolve_pattern(
    slide_spec: SlideSpec, patterns: list[Pattern], profile: TemplateProfile, variant: Variant,
) -> Pattern | None:
    """Раскладка для `slide_spec` — предпочитает `slide_spec.pattern_id`
    (Task 13: проставлен `plan.variants.apply_variant`, детерминированно по
    вместимости, и/или уточнён `plan.writer.pick_patterns` моделью), но
    НЕ доверяет ему слепо: `pattern_id` обязан существовать в ЭТОМ профиле,
    нести ТОТ ЖЕ `kind`, что и `slide_spec.kind`, и реально вмещать
    содержание (`fits().ok`, замер текста, которого нет ни у `apply_variant`,
    ни у `pick_patterns` — оба живут в `plan/`, не знающем координат/текстфита).
    Когда что-то из этого не выполняется — код берёт раскладку САМ,
    `_pick_pattern`, тем же путём, каким собирались `SAMPLE_SPEC`/`CARDS_
    SPEC` в `tests/compose/test_builder.py` (Task 9-10, до этой задачи;
    `pattern_id=None` там — обратная совместимость сохранена буквально)."""
    if slide_spec.pattern_id:
        candidate = next(
            (p for p in patterns if p.pattern_id == slide_spec.pattern_id and p.kind == slide_spec.kind),
            None,
        )
        if candidate is not None and fits(slide_spec, candidate, profile).ok:
            return candidate
    return _pick_pattern(slide_spec, patterns, profile, variant)


# ---------------------------------------------------------------------------
# Слайды-примеры шаблона
# ---------------------------------------------------------------------------


def _clear_sample_slides(prs) -> None:
    """Убирает все слайды-примеры шаблона из `p:sldIdLst` И из связей —
    рецепт python-pptx (`Part.drop_rel` снижает счётчик ссылок на часть
    слайда; когда он доходит до нуля, сама часть и её собственные
    relationships уходят вместе с ней). Мастера/лейауты/тема/media в
    `p:sldMasterIdLst` не затрагиваются вовсе — колода остаётся тем же
    файлом шаблона, лишь без слайдов-примеров."""
    xml_slides = prs.slides._sldIdLst
    for sld in list(xml_slides):
        prs.part.drop_rel(sld.rId)
        xml_slides.remove(sld)


def _find_layout(prs, layout_id: str):
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            stem = str(layout.part.partname).rsplit("/", 1)[-1]
            if stem.endswith(".xml"):
                stem = stem[: -len(".xml")]
            if stem == layout_id:
                return layout
    return None


# ---------------------------------------------------------------------------
# Адаптеры pydantic-зеркал профиля -> датаклассы разбора
# ---------------------------------------------------------------------------


def _box_from_model(b) -> Box:
    return Box(left=b.left, top=b.top, width=b.width, height=b.height)


def _pattern_from_model(model) -> Pattern:
    """`TemplateProfile.patterns` — JSON-совместимые зеркала (`PatternModel`,
    см. `template/profile.py`), не датаклассы `template.patterns.Pattern`,
    которых просит интерфейс задачи ("Consumes: ... Pattern"). Пересборка
    дешёвая (несколько списков небольшой длины на паттерн) — заново гонять
    майнинг паттернов по пакету ради тех же самых чисел не нужно."""
    slots = [
        PatternSlot(
            role=s.role, box=_box_from_model(s.box), size_pt=s.size_pt, color_hex=s.color_hex,
            align=s.align, max_chars=s.max_chars, wraps=s.wraps, sample_text=s.sample_text,
        )
        for s in model.slots
    ]
    repeat = None
    if model.repeat is not None:
        repeat = RepeatSpec(
            axis=model.repeat.axis, count=model.repeat.count, step=model.repeat.step,
            slot_roles=list(model.repeat.slot_roles), group_size=model.repeat.group_size,
        )
    decor = [
        DecorShape(
            kind=d.kind, box=_box_from_model(d.box), rotation=d.rotation, flip_h=d.flip_h, flip_v=d.flip_v,
            fill_hex=d.fill_hex, has_fill=d.has_fill, fill_kind=d.fill_kind,
            repeat_group=d.repeat_group, repeat_index=d.repeat_index,
        )
        for d in model.decor
    ]
    capacity = Capacity(
        max_items=model.capacity.max_items, max_chars_per_item=model.capacity.max_chars_per_item,
        max_bullets=model.capacity.max_bullets, max_series=model.capacity.max_series,
        max_rows=model.capacity.max_rows, max_cols=model.capacity.max_cols,
    )
    return Pattern(
        pattern_id=model.pattern_id, source_slide_index=list(model.source_slide_index),
        layout_id=model.layout_id, kind=model.kind, slots=slots, repeat=repeat, decor=decor,
        capacity=capacity, score=model.score, is_dark=model.is_dark,
    )


def _grid_from_model(model) -> Grid:
    columns = [
        ColumnAxis(center=c.center, count=c.count, confidence=c.confidence, source=c.source)
        for c in model.columns
    ]
    return Grid(
        margin_left=model.margin_left, margin_right=model.margin_right,
        margin_top=model.margin_top, margin_bottom=model.margin_bottom,
        columns=columns, gutter=model.gutter, anchors=dict(model.anchors),
        confidence=dict(model.confidence), skipped_no_box=model.skipped_no_box,
        native_guides_used=model.native_guides_used,
    )


# ---------------------------------------------------------------------------
# Отрисовка одного слота
# ---------------------------------------------------------------------------


def _primary_family(profile: TemplateProfile) -> str:
    return profile.type_scale.families[0] if profile.type_scale.families else "Arial"


def _line_spacing_for(role_hint: str, profile: TemplateProfile) -> float:
    if role_hint in _HEADING_ROLES:
        return profile.type_scale.heading_line_spacing
    return profile.type_scale.body_line_spacing


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast_ratio_from_luminance(l_a: float, l_b: float) -> float:
    """Контраст WCAG между двумя ОТНОСИТЕЛЬНЫМИ ЯРКОСТЯМИ (не цветами) —
    та же формула, что `naming.contrast_ratio`, но берёт готовую яркость
    напрямую: фон под слотом не всегда несёт цвет (фоновое фото даёт только
    усреднённую яркость, см. `layouts.Background.luminance`), контраст
    WCAG зависит только от яркости, не от оттенка, так что усреднённого
    значения достаточно, чтобы честно посчитать пару."""
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


def _best_contrast_color(candidate_hex: str, bg_luminance: float, profile: TemplateProfile) -> str:
    """Цвет текста, дающий контраст WCAG не ниже `naming.MIN_CONTRAST`
    (4.5:1) к ФАКТИЧЕСКОЙ яркости фона под слотом — Task 10 отчёт, находка
    №1: раньше решение было бинарным ("тёмный/светлый фон" ->
    `surface`/`on_surface` полюс), не численным, и не проверяло реальный
    контраст итоговой пары — раскладка, снятая со светлого примера и
    положенная на тёмный макет, несла `is_dark=False`, полюс не менялся,
    чёрный текст ложился на тёмно-фиолетовый фон.

    Если `candidate_hex` (роль, которую предложил `_color_for_role`) уже
    даёт нужный контраст — используется он, чтобы не менять цвет там, где
    и так всё читаемо (кегль/роль сохраняют смысл: kpi_value остаётся
    брендовым цветом, если он и так контрастен). Иначе — берётся цвет
    ИЗ ПАЛИТРЫ ШАБЛОНА (`profile.palette_roles`, не произвольный чёрный/
    белый — брифом: "выбирай из палитры шаблона"), дающий НАИЛУЧШИЙ
    контраст к этому фону; если даже лучший из палитры не дотягивает до
    4.5:1 (редкий случай — фон декора вне откалиброванной пары
    surface/on_surface), берётся всё равно лучший из худших — это не хуже
    прежнего поведения и не изобретает цвет вне дизайн-системы шаблона."""
    palette = list(dict.fromkeys(v for v in profile.palette_roles.values() if v))
    if not palette:
        return candidate_hex

    def contrast(hex_c: str) -> float:
        return _contrast_ratio_from_luminance(bg_luminance, _relative_luminance(hex_c))

    if candidate_hex in palette and contrast(candidate_hex) >= MIN_CONTRAST:
        return candidate_hex
    return max(palette, key=contrast)


def _contains(outer: Box, inner: Box, tolerance: float = 0.01) -> bool:
    """`inner` целиком лежит внутри `outer` (с допуском на округление) —
    геометрический тест "слот стоит на этой плашке декора"."""
    return (
        outer.left - tolerance <= inner.left
        and outer.top - tolerance <= inner.top
        and inner.left + inner.width <= outer.left + outer.width + tolerance
        and inner.top + inner.height <= outer.top + outer.height + tolerance
    )


def _plaque_under(box: Box, decor: list[DecorShape]) -> DecorShape | None:
    """Самая маленькая (самая специфичная — не подложка всего слайда)
    плашка декора паттерна, полностью содержащая `box` и несущая цвет
    заливки — кандидат на "настоящий локальный фон" под содержимым слота
    (см. `_local_background_is_dark`)."""
    candidates = [
        d for d in decor
        if d.kind == "shape" and d.has_fill and d.fill_hex and _contains(d.box, box)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda d: d.box.width * d.box.height)


def _layout_entry(profile: TemplateProfile, layout_id: str) -> LayoutEntryModel | None:
    return next((entry for entry in profile.layouts if entry.layout_id == layout_id), None)


def _local_background_luminance(
    slot_box: Box, pattern: Pattern, layout_bg_luminance: float | None,
) -> float:
    """Относительная яркость WCAG фона НЕПОСРЕДСТВЕННО под слотом.

    Порядок источников (Task 10 отчёт, находка №1 — правка по итогам
    визуального ревью v3, ЛЦТ2026, "Риски раскатки (детали)": чёрный текст
    на тёмно-фиолетовом фоне; раскладка снята со светлого слайда-примера
    (`pattern.is_dark=False`), но положена на ТЁМНЫЙ макет шаблона):

    1. Плашка декора НЕПОСРЕДСТВЕННО под слотом (`_plaque_under`), если
       она есть — текст лежит на плашке, а не на фоне слайда, фоном
       служит ОНА (находка визуального ревью v2, ЛЦТ2026, "cards": белая
       плашка поверх тёмного фона паттерна).
    2. Иначе — фон МАКЕТА, на который слайд реально ставится
       (`layout_bg_luminance`, из `profile.layouts`, посчитан надёжно: с
       учётом наследования от мастера, градиентов и фоновых фото, Task
       6/8) — ИСТОЧНИК ИСТИНЫ, не `pattern.is_dark`: раскладка снята со
       слайда-примера и может не совпадать по фону с макетом, на который
       её ставит подбор паттерна.
    3. Только если фон макета в принципе не резолвился (`None` — не
       должно случаться на профиле, разобранном с того же файла, но
       честный крайний случай) — `pattern.is_dark`, как единственный
       оставшийся сигнал."""
    plaque = _plaque_under(slot_box, pattern.decor)
    if plaque is not None and plaque.fill_hex:
        return _relative_luminance(plaque.fill_hex)
    if layout_bg_luminance is not None:
        return layout_bg_luminance
    return 0.0 if pattern.is_dark else 1.0


def _color_for_role(role_hint: str, slot: PatternSlot, profile: TemplateProfile, bg_luminance: float) -> str:
    values = set(profile.palette_roles.values())
    if slot.color_hex and slot.color_hex in values:
        return _best_contrast_color(slot.color_hex, bg_luminance, profile)
    color = profile.palette_roles.get(_ROLE_COLOR.get(role_hint, "on_surface"))
    if color:
        return _best_contrast_color(color, bg_luminance, profile)
    return next(iter(values)) if values else "#000000"


# ---------------------------------------------------------------------------
# Наложение слота на декор раскладки
# ---------------------------------------------------------------------------

# Порог "площадь пересечения уже значимая, а не игра округления" — доля
# площади МЕНЬШЕЙ из двух фигур (тот же принцип, что `_FIT_TOLERANCE_IN`/
# `_LINE_THICKNESS_SHARE` в этом файле: округлая отсечка на порядок больше
# погрешности вычислений с плавающей точкой, не подгонка под конкретный
# файл). 10% площади меньшей фигуры — уже заметное на глаз наложение
# (буквы текста реально ложатся на чужой декор), пересечение на доли
# процента — то же самое, что перекрытие рамок при округлении EMU.
_OVERLAP_MIN_AREA_SHARE = 0.1


def _overlap_ratio(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.left + a.width, b.left + b.width) - max(a.left, b.left))
    iy = max(0.0, min(a.top + a.height, b.top + b.height) - max(a.top, b.top))
    inter = ix * iy
    if inter <= 0.0:
        return 0.0
    return inter / min(a.width * a.height, b.width * b.height)


def _colliding_decor(box: Box, decor: list[DecorShape]) -> DecorShape | None:
    """Первая закрашенная фигура декора, которая ЧАСТИЧНО перекрывает
    `box` площадью выше `_OVERLAP_MIN_AREA_SHARE` — коллизия, которую
    нужно чинить (Task 10 отчёт, находка №2: заголовок налез на плашку
    декора). ПОЛНОЕ содержание (`_contains` в любую сторону) — не
    коллизия, а легитимный случай "слот стоит на своей плашке-фоне"
    (тот же тест, что уже применяет `_plaque_under` для контраста)."""
    for shape in decor:
        if not shape.has_fill:
            continue
        if _contains(shape.box, box) or _contains(box, shape.box):
            continue
        if _overlap_ratio(box, shape.box) > _OVERLAP_MIN_AREA_SHARE:
            return shape
    return None


def _avoid_decor_overlap(box: Box, decor: list[DecorShape], grid: Grid) -> Box:
    """Сдвигает `box`, если он значимо наезжает на декор раскладки — ТЗ
    (Task 10 отчёт, находка №2): "перед тем как положить текст в слот,
    проверь, не занято ли это место декором... если занято — сдвигай".
    Пробует по очереди: вниз (очистить нижний край мешающей фигуры),
    вправо, влево — первый сдвиг, который снимает коллизию и остаётся в
    полях шаблона, побеждает. Если ни один не помогает (декор шире всего
    слота целиком) — возвращает `box` как есть, это честный крайний
    случай, не более скрытый, чем прежнее поведение (наложение оставалось
    всегда)."""
    blocking = _colliding_decor(box, decor)
    if blocking is None:
        return box
    d = blocking.box
    margin_bottom_limit = 1.0 - grid.margin_bottom
    margin_right_limit = 1.0 - grid.margin_right

    below = replace(box, top=d.top + d.height)
    if below.top + below.height <= margin_bottom_limit + 0.001 and _colliding_decor(below, decor) is None:
        return below

    right = replace(box, left=d.left + d.width)
    if right.left + right.width <= margin_right_limit + 0.001 and _colliding_decor(right, decor) is None:
        return right

    left = replace(box, left=max(grid.margin_left, d.left - box.width))
    if left.left >= grid.margin_left - 0.001 and _colliding_decor(left, decor) is None:
        return left

    return box


def _shrink_sequence(profile: TemplateProfile, slot_size_pt: float) -> list[float]:
    """Кегли-кандидаты по убыванию, начиная с собственного кегля слота,
    затем ступени `TypeScale` не выше него и не ниже "caption" (брифом: "не
    ниже подписи"). И `PatternSlot.size_pt`, и `TypeScale.steps` нормированы
    к эталонному холсту 13.333″ (`Canvas.norm` — та же нормировка, что и в
    `typography.py`/`patterns._shape_dominant_size`: `sz_raw/100*canvas.
    norm`), а рисовать нужно РЕАЛЬНЫЙ кегль ЭТОГО холста — денормируем оба
    источника одним и тем же коэффициентом (`TemplateProfile.denorm_pt`/
    `type_scale_pt`, единственное место в проекте, которое имеет право
    делить нормированный кегль на `canvas_norm`) один раз здесь, а не
    порознь у вызывающего."""
    size_pt = profile.denorm_pt(slot_size_pt)
    raw_steps = {name: profile.type_scale_pt(name, 0.0) for name in _SHRINK_STEPS}
    caption_pt = raw_steps["caption"]

    seq = [size_pt]
    for name in _SHRINK_STEPS:
        value = raw_steps[name]
        if 0 < value < seq[-1] - 0.05:
            seq.append(value)
    if caption_pt > 0 and abs(seq[-1] - caption_pt) > 0.05:
        seq.append(caption_pt)
    return seq


def _joined_text(paragraphs: list[Paragraph]) -> str:
    return "\n".join(p.text for p in paragraphs)


def _split_back(text: str, original: list[Paragraph]) -> list[Paragraph]:
    bullet_flags = [p.bullet for p in original]
    last_flag = bullet_flags[-1] if bullet_flags else False
    lines = text.split("\n")
    return [Paragraph(line, bullet=(bullet_flags[i] if i < len(bullet_flags) else last_flag)) for i, line in enumerate(lines)]


# Порог "усечение ещё косметическое, а не разрушительное" — доля исходной
# длины текста (в знаках, без многоточия), которую усечение ОБЯЗАНО
# сохранить, иначе оно не применяется вовсе (см. `_is_cosmetic_truncation`).
# Находка визуального ревью (отчёт задачи): «Доработка для закупок…» на
# VK Tech сохраняла ~40% исходной фразы и уже читалась как брак, а «…» без
# единого слова содержания — 0%. 0.7 — обоснование: усечение, вырезающее
# МЕНЬШЕ 30% знаков, типично отрезает только хвостовое уточнение/придаточное
# (для этих коротких карточных фраз главное — подлежащее+сказуемое —
# статистически укладывается в первые 60-70% предложения), смысл остаётся
# читаемым. Усечение, которое вырезало бы больше — уже не "подрезали хвост",
# а "переписали контент огрызком" — хуже, чем оставить текст целиком и
# позволить ему видимо вылезти за рамку (это поймает будущий аудит/
# песочница, Task 10a, а не тихо спрятанный обрубок).
_COSMETIC_TRUNCATION_MIN_RETAINED = 0.7


def _is_cosmetic_truncation(original: str, truncated: str) -> bool:
    """Сохраняет ли `truncated` (результат `_truncate_to_fit`, включая
    завершающее «…», если оно есть) не меньше `_COSMETIC_TRUNCATION_MIN_
    RETAINED` доли исходной длины `original` (в знаках)."""
    if not original:
        return True
    prefix = truncated[:-1] if truncated.endswith("…") else truncated
    return len(prefix) / len(original) >= _COSMETIC_TRUNCATION_MIN_RETAINED


def _truncate_to_fit(
    text: str, family: str, size_pt: float, box_width_in: float, box_height_in: float, line_spacing: float,
) -> tuple[str, bool]:
    """Двоичный поиск самого длинного слова-выровненного префикса, который
    ещё влезает по высоте (брифом: "усекай, но оставь след" — многоточие,
    не тихий обрыв слова). Границы абзацев (`\\n`) при этом не сохраняются
    отдельно — усечение схлопывает содержание в одну проверяемую строку;
    честно объявленное упрощение: сюда доходит только контент, для
    которого ужимание по всей шкале уже не помогло (редкий, аварийный
    путь — находка о самом факте усечения важнее аккуратности разбивки
    остатка на исходные абзацы).

    Возвращает КАНДИДАТА на усечение и было ли оно вообще нужно — не
    решает, принять ли его: разрушительное усечение (меньше
    `_COSMETIC_TRUNCATION_MIN_RETAINED` исходной длины, включая усечение до
    голого «…») ОТКЛОНЯЕТ вызывающий код (`_draw_slot`,
    `_is_cosmetic_truncation`) — пустая/усечённая-до-точек карточка хуже
    видимого переполнения (находка визуального ревью, отчёт задачи)."""
    metrics = measure(text, family, size_pt, box_width_in, line_spacing=line_spacing)
    if metrics.height_in <= box_height_in + _FIT_TOLERANCE_IN:
        return text, False

    words = text.split()
    if not words:
        return text, False

    lo, hi, best = 0, len(words), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = " ".join(words[:mid])
        marked = candidate + "…" if mid < len(words) else candidate
        m = measure(marked, family, size_pt, box_width_in, line_spacing=line_spacing)
        if m.height_in <= box_height_in + _FIT_TOLERANCE_IN:
            best = marked
            lo = mid + 1
        else:
            hi = mid - 1
    return (best or "…"), True


def _apply_bullet(paragraph, bullet_char: str, family: str) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    for tag in ("a:buChar", "a:buAutoNum", "a:buNone"):
        el = p_pr.find(qn(tag))
        if el is not None:
            p_pr.remove(el)
    bu_font = etree.SubElement(p_pr, qn("a:buFont"))
    bu_font.set("typeface", family)
    bu_char = etree.SubElement(p_pr, qn("a:buChar"))
    bu_char.set("char", bullet_char)


def _draw_slot(
    slide, slide_spec: SlideSpec, content: SlotContent, profile: TemplateProfile, family: str,
    bullet_char: str, canvas_width_emu: int, canvas_height_emu: int, bg_luminance: float,
) -> None:
    box = content.slot.box
    left = round(box.left * canvas_width_emu)
    top = round(box.top * canvas_height_emu)
    width = max(1, round(box.width * canvas_width_emu))
    height = max(1, round(box.height * canvas_height_emu))

    textbox = slide.shapes.add_textbox(Emu(left), Emu(top), Emu(width), Emu(height))
    tf = textbox.text_frame
    tf.word_wrap = True
    # python-pptx даёт текстовой рамке ненулевые поля по умолчанию (0.1"
    # слева/справа, 0.05" сверху/снизу, OOXML `a:bodyPr` lIns/rIns/tIns/
    # bIns) — `measure()` меряет по ПОЛНОЙ ширине/высоте фигуры (см.
    # `box_width_in`/`box_height_in` ниже), без вычета полей. Найдено на
    # повторном визуальном ревью (после установки Play): заголовок стал
    # переноситься на строку больше, чем предсказал замер, и наезжать на
    # содержимое ниже — бокс, который меряет `measure()`, обязан совпадать
    # с боксом, в который реально льётся текст у PowerPoint/LibreOffice.
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Emu(0)

    line_spacing = _line_spacing_for(content.role_hint, profile)
    box_width_in = width / EMU_PER_INCH
    box_height_in = height / EMU_PER_INCH

    full_text = _joined_text(content.paragraphs)
    sizes = _shrink_sequence(profile, content.slot.size_pt)

    chosen_size = sizes[-1]
    chosen_text = full_text
    fit_found = False
    for size_pt in sizes:
        metrics = measure(full_text, family, size_pt, box_width_in, line_spacing=line_spacing)
        if metrics.height_in <= box_height_in + _FIT_TOLERANCE_IN:
            chosen_size = size_pt
            fit_found = True
            break

    if not fit_found:
        truncated_text, truncated = _truncate_to_fit(
            full_text, family, chosen_size, box_width_in, box_height_in, line_spacing,
        )
        if truncated and _is_cosmetic_truncation(full_text, truncated_text):
            chosen_text = truncated_text
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» усечён — "
                f"не влезает даже кеглем подписи ({chosen_size:.1f}pt)."
            )
        elif truncated:
            # Усечение вырезало бы больше _COSMETIC_TRUNCATION_MIN_RETAINED
            # исходного текста (в пределе — до голого «…» или пустоты) —
            # находка ревью: пустая/усечённая-до-точек карточка хуже
            # переполненной. Оставляем текст ЦЕЛИКОМ на минимальном кегле:
            # он видимо вылезет за рамку слота, зато не потеряет смысл, и
            # это честно ловит finding, а не молча прячет контент.
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» не помещается даже "
                f"кеглем подписи ({chosen_size:.1f}pt), а усечение вырезало бы больше "
                f"{(1 - _COSMETIC_TRUNCATION_MIN_RETAINED):.0%} содержания — оставлен целиком "
                "(слот переполнен, эта раскладка не подходит для этого содержания)."
            )

    display_paragraphs = _split_back(chosen_text, content.paragraphs)
    color_hex = _color_for_role(content.role_hint, content.slot, profile, bg_luminance)
    align = _ALIGN_MAP.get(content.slot.align, PP_ALIGN.LEFT)
    bold = profile.type_scale.bold_is_idiomatic and content.role_hint in _HEADING_ROLES

    for i, para in enumerate(display_paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = para.text
        run.font.size = Pt(chosen_size)
        run.font.name = family
        run.font.bold = bold
        run.font.color.rgb = RGBColor.from_string(color_hex.lstrip("#"))
        if para.bullet:
            _apply_bullet(p, bullet_char, family)


# ---------------------------------------------------------------------------
# Путь сохранения
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^0-9a-zA-Zа-яА-ЯёЁ]+")


def _output_path(spec: DeckSpec, template_path: Path, variant: Variant) -> Path:
    try:
        settings = Settings.load(APP_YAML_PATH)
        base_dir = settings.paths.artifacts
        if not base_dir.is_absolute():
            base_dir = APP_YAML_PATH.parents[1] / base_dir
    except Exception:
        base_dir = APP_YAML_PATH.parents[1] / "artifacts"

    slug = _SLUG_RE.sub("-", spec.title).strip("-").lower() or "deck"
    template_slug = _SLUG_RE.sub("-", template_path.stem).strip("-").lower() or "template"
    return base_dir / f"{slug}__{template_slug}__{variant.value}.pptx"
