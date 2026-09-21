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
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from deckforge.compose.blocks import Paragraph, SlotContent, assign_content, find_bullet_char
from deckforge.compose.decor import apply_decor
from deckforge.compose.textfit import measure, register_template_fonts
from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.plan.spec import BulletBlock, CardBlock, DeckSpec, KpiBlock, QuoteBlock, SlideSpec, TextBlock
from deckforge.settings import Settings
from deckforge.template.grid import ColumnAxis, Grid
from deckforge.template.patterns import Capacity, DecorShape, Pattern, PatternSlot, RepeatSpec
from deckforge.template.profile import TemplateProfile

APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"
EMU_PER_INCH = 914400


class Variant(Enum):
    """Плотность/визуальность сборки — влияет только на выбор ПАТТЕРНА
    среди нескольких кандидатов одного `kind` (см. `_pick_pattern`):
    `visual` предпочитает раскладки с бОльшим числом декоративных фигур,
    `dense` — раскладки победнее декором (обычно вместительнее по тексту).
    Первый грубый проход — настоящий подбор паттерна по смыслу содержания
    сделает Task 13 (`pattern_picker`, см. `config/app.yaml`)."""

    dense = "dense"
    visual = "visual"


@dataclass(frozen=True)
class Fit:
    """Лезет ли содержание слайда в раскладку паттерна БЕЗ ужимания —
    используется на выборе паттерна (см. `_pick_pattern`) и остаётся
    доступным вызывающему коду (Task 10a: "ни одна не подходит — слайд
    уходит в песочницу"). `overflow_ratio` — во сколько раз (сверх 1.0)
    самый переполненный слот превышает свою высоту на СОБСТВЕННОМ кегле
    паттерна (0.0 — не переполнен нигде); `reason` — какой слот и почему."""

    ok: bool
    overflow_ratio: float
    reason: str


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
        pattern = _pick_pattern(slide_spec, patterns, profile, variant)
        if pattern is None:
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: для kind={slide_spec.kind!r} не нашлось ни одного "
                "паттерна этого шаблона — слайд не собран."
            )
            continue
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
    apply_decor(slide, pattern.decor, canvas_width_emu, canvas_height_emu)

    grid = _grid_from_model(profile.grid)
    family = _primary_family(profile)
    for content in assign_content(slide_spec, pattern, grid):
        _draw_slot(
            slide, slide_spec, content, profile, family, bullet_char, canvas_width_emu, canvas_height_emu,
            pattern.is_dark,
        )


def fits(slide_spec: SlideSpec, pattern: Pattern, profile: TemplateProfile) -> Fit:
    """Лезет ли содержание `slide_spec` в `pattern` БЕЗ ужимания шрифта —
    более лёгкая проверка, чем реальная укладка (`place_slide`), для
    выбора паттерна ДО того, как тратить время на построение слайда."""
    grid = _grid_from_model(profile.grid)
    assignments = assign_content(slide_spec, pattern, grid)
    canvas_width_in = profile.canvas_width_emu / EMU_PER_INCH
    canvas_height_in = profile.canvas_height_emu / EMU_PER_INCH
    family = _primary_family(profile)
    norm = 12192000 / profile.canvas_width_emu if profile.canvas_width_emu else 1.0

    worst_ratio = 0.0
    worst_reason = ""
    for content in assignments:
        text = _joined_text(content.paragraphs)
        if not text.strip():
            continue
        box_width_in = content.slot.box.width * canvas_width_in
        box_height_in = content.slot.box.height * canvas_height_in
        if box_height_in <= 0:
            continue
        line_spacing = _line_spacing_for(content.role_hint, profile)
        # `PatternSlot.size_pt` нормирован к эталонному холсту (см. докстроку
        # `_shrink_sequence`) — денормируем тем же коэффициентом.
        metrics = measure(text, family, content.slot.size_pt / norm, box_width_in, line_spacing=line_spacing)
        ratio = metrics.height_in / box_height_in - 1.0
        if ratio > worst_ratio:
            worst_ratio = ratio
            worst_reason = f"слот «{content.role_hint}» переполнен на {ratio:.0%} на кегле паттерна"

    missing = _missing_signals(slide_spec, assignments)
    if missing:
        worst_ratio = max(worst_ratio, 1.0)
        worst_reason = f"нет слота под роль(и): {', '.join(missing)}"

    ok = worst_ratio <= 0.0
    return Fit(ok=ok, overflow_ratio=max(worst_ratio, 0.0), reason=worst_reason or "содержание помещается")


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
    candidates = [p for p in patterns if p.kind == slide_spec.kind]
    if not candidates:
        return None

    def rank(p: Pattern):
        fit = fits(slide_spec, p, profile)
        visual_bias = len(p.decor) if variant is Variant.visual else -len(p.decor)
        return (0 if fit.ok else 1, fit.overflow_ratio, -p.score, -visual_bias)

    return min(candidates, key=rank)


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
    columns = [ColumnAxis(center=c.center, count=c.count, confidence=c.confidence) for c in model.columns]
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


def _contrast_adjusted(color_hex: str, is_dark: bool, profile: TemplateProfile) -> str:
    """Раскладка живёт СВОИМ фоном (`pattern.is_dark`), который может не
    совпадать с доминирующим фоном шаблона, на котором `palette_roles.
    surface`/`on_surface` калибровались (`naming.py`) — увидено руками на
    рендере: "cards"-паттерн со светлым фоном получал белый ("on_surface")
    текст того же цвета, что и фон, буквально нечитаемый. Меняем местами
    ТОЛЬКО когда цвет — один из двух полюсов пары фон/текст
    (`surface`/`on_surface`): более тёмный полюс — для светлого фона,
    более светлый — для тёмного; остальные роли палитры (brand/accent/
    muted/...) не трогаем — они не привязаны к контрасту с фоном тем же
    образом."""
    surface = profile.palette_roles.get("surface")
    on_surface = profile.palette_roles.get("on_surface")
    if not surface or not on_surface or color_hex not in (surface, on_surface):
        return color_hex
    dark_pole, light_pole = (
        (surface, on_surface) if _relative_luminance(surface) < _relative_luminance(on_surface)
        else (on_surface, surface)
    )
    return light_pole if is_dark else dark_pole


def _color_for_role(role_hint: str, slot: PatternSlot, profile: TemplateProfile, is_dark: bool) -> str:
    values = set(profile.palette_roles.values())
    if slot.color_hex and slot.color_hex in values:
        return _contrast_adjusted(slot.color_hex, is_dark, profile)
    color = profile.palette_roles.get(_ROLE_COLOR.get(role_hint, "on_surface"))
    if color:
        return _contrast_adjusted(color, is_dark, profile)
    return next(iter(values)) if values else "#000000"


def _shrink_sequence(profile: TemplateProfile, slot_size_pt: float) -> list[float]:
    """Кегли-кандидаты по убыванию, начиная с собственного кегля слота,
    затем ступени `TypeScale` не выше него и не ниже "caption" (брифом: "не
    ниже подписи"). И `PatternSlot.size_pt`, и `TypeScale.steps` нормированы
    к эталонному холсту 13.333″ (`Canvas.norm` — та же нормировка, что и в
    `typography.py`/`patterns._shape_dominant_size`: `sz_raw/100*canvas.
    norm`), а рисовать нужно РЕАЛЬНЫЙ кегль ЭТОГО холста — денормируем оба
    источника одним и тем же коэффициентом (`canvas.norm = 12192000 /
    canvas_width_emu`) один раз здесь, а не порознь у вызывающего."""
    norm = 12192000 / profile.canvas_width_emu if profile.canvas_width_emu else 1.0
    size_pt = slot_size_pt / norm
    raw_steps = {name: profile.type_scale.steps.get(name, 0.0) / norm for name in _SHRINK_STEPS}
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
    остатка на исходные абзацы)."""
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
    bullet_char: str, canvas_width_emu: int, canvas_height_emu: int, is_dark: bool,
) -> None:
    box = content.slot.box
    left = round(box.left * canvas_width_emu)
    top = round(box.top * canvas_height_emu)
    width = max(1, round(box.width * canvas_width_emu))
    height = max(1, round(box.height * canvas_height_emu))

    textbox = slide.shapes.add_textbox(Emu(left), Emu(top), Emu(width), Emu(height))
    tf = textbox.text_frame
    tf.word_wrap = True

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
        chosen_text, truncated = _truncate_to_fit(
            full_text, family, chosen_size, box_width_in, box_height_in, line_spacing,
        )
        if truncated:
            slide_spec.findings.append(
                f"Слайд {slide_spec.index}: текст слота «{content.role_hint}» усечён — "
                f"не влезает даже кеглем подписи ({chosen_size:.1f}pt)."
            )

    display_paragraphs = _split_back(chosen_text, content.paragraphs)
    color_hex = _color_for_role(content.role_hint, content.slot, profile, is_dark)
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
