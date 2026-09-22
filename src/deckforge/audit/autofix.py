"""Механическая починка находок детерминированного аудита (Task 16, API/
веб-интерфейс).

`audit.deterministic` уже несёт признак `Finding.fixable` и текстовую
подсказку `Finding.fix_hint` для человека — но ни один модуль до этой
задачи не выполняет саму починку: подсказка адресована человеку с мышью, не
коду. Этому модулю нужен API-слой (`POST /api/decks/{id}/fix`,
`autofix: bool = True` у `POST /api/decks`), поэтому починка обязана быть
исполняемым кодом, а не только текстом.

## Границы честности

`apply_fixes` работает НАПРЯМУЮ поверх уже собранного `.pptx` (через
`python-pptx`, тот же слой, каким открыт `audit.deterministic`), а не
пересобирает колоду с нуля из `DeckSpec` — правка точечная (сдвинуть
фигуру/поменять кегль/перекрасить), а не повторный проход раскладки.

Покрыты все тринадцать `check_id`, для которых `_finding(...)` в
`audit.deterministic` выставляет `fixable=True` (L01, L02, L03, L04, L05,
L06, L07, T01, T02, T03, T05, T06, I05) — `SUPPORTED_CHECKS` ниже. Находка
с `check_id` вне этого множества или без `shape_ref`/`box`, нужных её
починке, возвращается в `skipped` как есть: `apply_fixes` никогда не
делает вид, что починила то, чего не коснулась.

Каждая починка — эвристика, не идеальное решение вёрстки (это работа
`compose.builder`, не аудита): цель — снять СИМПТОМ, из-за которого
сработала проверка (фигура ушла на просроченный кегль/цвет/место), чтобы
повторный `run_deterministic` больше не находил ЭТУ находку, а не
воспроизвести то единственное расположение, которое выбрал бы генератор
заново.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from deckforge.audit.findings import Finding
from deckforge.compose.colorpick import slide_background_luminance
from deckforge.compose.textfit import measure
from deckforge.template.profile import TemplateProfile

# Дословно множество check_id, у которых `audit.deterministic._finding(...)`
# передаёт `fixable=True` — см. докстроку модуля.
SUPPORTED_CHECKS = frozenset({
    "L01", "L02", "L03", "L04", "L05", "L06", "L07",
    "T01", "T02", "T03", "T05", "T06", "I05",
})

# Нижний пол кегля при пошаговом уменьшении (L03/L04) — тот же порядок
# величины, что "caption"/"micro" ступеней типовой шкалы; ниже это уже не
# текст, а шум.
_MIN_FONT_PT = 8.0


@dataclass
class AutofixResult:
    """Итог одного вызова `apply_fixes` — что реально изменилось на диске."""

    applied: list[str] = field(default_factory=list)  # id обработанных находок
    skipped: list[str] = field(default_factory=list)   # id находок, которые чинить нечем
    changed: bool = False


def _flatten(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _flatten(shape.shapes)


def _find_shape(slide, shape_id: str):
    for shape in _flatten(slide.shapes):
        try:
            if str(shape.shape_id) == shape_id:
                return shape
        except Exception:  # noqa: BLE001 — фигура без валидного id не адресуема
            continue
    return None


def _shape_id_from_ref(shape_ref: str) -> str:
    return shape_ref.split(":", 1)[0]


def _relative_luminance(hex_color: str) -> float:
    def lin(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(l_a: float, l_b: float) -> float:
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


def _nearest_palette_hex(hex_color: str, profile: TemplateProfile) -> str:
    candidates = [v.lstrip("#") for v in profile.palette_roles.values() if v]
    if not candidates:
        return hex_color
    hex_color = hex_color.lstrip("#")

    def dist(a: str, b: str) -> int:
        ar, ag, ab = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        br, bg, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
        return (ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2

    return min(candidates, key=lambda c: dist(hex_color, c))


def _nearest_type_scale_pt(size_pt: float, profile: TemplateProfile) -> float:
    steps = [profile.denorm_pt(v) for v in profile.type_scale.steps.values() if v]
    if not steps:
        return size_pt
    return min(steps, key=lambda s: abs(s - size_pt))


def _all_runs(shape):
    if not shape.has_text_frame:
        return
    for paragraph in shape.text_frame.paragraphs:
        yield from paragraph.runs


# ---------------------------------------------------------------------------
# Починки по геометрии
# ---------------------------------------------------------------------------

def _fix_L01(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Вернуть фигуру целиком на холст — сдвинуть и, если она сама шире/выше
    холста, уменьшить до размера холста с тем же отступом с обеих сторон."""
    cw, ch = profile.canvas_width_emu, profile.canvas_height_emu
    left, top, width, height = shape.left, shape.top, shape.width, shape.height
    if width > cw:
        width = cw
    if height > ch:
        height = ch
    left = max(0, min(left, cw - width))
    top = max(0, min(top, ch - height))
    shape.left, shape.top, shape.width, shape.height = Emu(left), Emu(top), Emu(width), Emu(height)
    return True


def _fix_L06(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Сдвинуть содержимое внутрь полей шаблона (без изменения размера)."""
    cw, ch = profile.canvas_width_emu, profile.canvas_height_emu
    ml = int(profile.grid.margin_left * cw)
    mr = int(profile.grid.margin_right * cw)
    mt = int(profile.grid.margin_top * ch)
    mb = int(profile.grid.margin_bottom * ch)
    left = max(ml, min(shape.left, cw - mr - shape.width))
    top = max(mt, min(shape.top, ch - mb - shape.height))
    shape.left, shape.top = Emu(left), Emu(top)
    return True


def _fix_L02(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Раздвинуть фигуру от зоны наложения (`finding.box` — пересечение
    двух фигур, не собственная рамка `shape`) — сдвигаем фигуру за
    ближайший край зоны пересечения по той оси, где сдвиг короче."""
    if finding.box is None:
        return False
    cw, ch = profile.canvas_width_emu, profile.canvas_height_emu
    overlap = finding.box
    shape_left = shape.left / cw
    shape_top = shape.top / ch
    dx_right = overlap.right - shape_left  # сдвинуть вправо, чтобы уйти за правый край зоны
    dx_left = shape_left + (shape.width / cw) - overlap.left  # сдвинуть влево
    dy_down = overlap.bottom - shape_top
    dy_up = shape_top + (shape.height / ch) - overlap.top
    candidates = [
        (abs(dx_right), "right", dx_right), (abs(dx_left), "left", dx_left),
        (abs(dy_down), "down", dy_down), (abs(dy_up), "up", dy_up),
    ]
    _, axis, magnitude = min(candidates, key=lambda c: c[0])
    pad = int(0.01 * cw)
    if axis == "right":
        shape.left = Emu(min(cw - shape.width, shape.left + int(magnitude * cw) + pad))
    elif axis == "left":
        shape.left = Emu(max(0, shape.left - int(magnitude * cw) - pad))
    elif axis == "down":
        shape.top = Emu(min(ch - shape.height, shape.top + int(magnitude * ch) + pad))
    else:
        shape.top = Emu(max(0, shape.top - int(magnitude * ch) - pad))
    return True


def _fix_L05(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Прижать левый край блока к ближайшей направляющей восстановленной
    сетки (полю или колонне)."""
    cw = profile.canvas_width_emu
    anchors = [profile.grid.margin_left, 1 - profile.grid.margin_right] + [
        c.center for c in profile.grid.columns
    ]
    if not anchors:
        return False
    current = shape.left / cw
    nearest = min(anchors, key=lambda a: abs(a - current))
    shape.left = Emu(max(0, int(nearest * cw)))
    return True


def _fix_L07(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Вписать картинку в рамку с сохранением родных пропорций — меняем
    меньшее из двух измерений так, чтобы соотношение сторон совпало с
    родным, оставляя центр рамки на месте."""
    try:
        native_w, native_h = shape.image.size
    except Exception:  # noqa: BLE001 — не картинка/повреждённые байты
        return False
    if not native_w or not native_h:
        return False
    native_aspect = native_w / native_h
    placed_aspect = shape.width / shape.height if shape.height else native_aspect
    cx = shape.left + shape.width / 2
    cy = shape.top + shape.height / 2
    if placed_aspect > native_aspect:
        new_width = int(shape.height * native_aspect)
        shape.width = Emu(new_width)
        shape.left = Emu(int(cx - new_width / 2))
    else:
        new_height = int(shape.width / native_aspect)
        shape.height = Emu(new_height)
        shape.top = Emu(int(cy - new_height / 2))
    return True


def _shrink_text(shape, profile: TemplateProfile) -> bool:
    """Уменьшить кегль всей текстовой фигуры до ступени шкалы шаблона, при
    которой замеренный текст (`compose.textfit.measure` — тот же замер, что
    и `compose.builder._shrink_sequence` использует при УКЛАДКЕ, не своя
    отдельная оценка) укладывается в доступную высоту рамки; если ни одна
    ступень не помогла — падает до `_MIN_FONT_PT`. Один кегль на всю фигуру
    (не по run'ам отдельно) — `measure()` меряет фигуру целиком, и разнобой
    кеглей внутри одной текстовой рамки сам по себе нарушал бы T02/шкалу."""
    if not shape.has_text_frame:
        return False
    text_frame = shape.text_frame
    full_text = "\n".join(p.text for p in text_frame.paragraphs)
    if not full_text.strip():
        return False

    current_pt = next((r.font.size.pt for r in _all_runs(shape) if r.font.size is not None), None)
    if current_pt is None:
        return False
    family = next((r.font.name for r in _all_runs(shape) if r.font.name), None)
    family = family or (profile.type_scale.families[0] if profile.type_scale.families else "Arial")

    width_in = shape.width / 914400
    available_h_in = shape.height / 914400
    steps = sorted({profile.denorm_pt(v) for v in profile.type_scale.steps.values() if v}, reverse=True)
    candidates = [s for s in steps if s < current_pt - 0.1]

    chosen = _MIN_FONT_PT
    for candidate in candidates:
        candidate = max(_MIN_FONT_PT, candidate)
        metrics = measure(full_text, family, candidate, width_in)
        if metrics.height_in <= available_h_in:
            chosen = candidate
            break
    else:
        # Ни одна ступень шкалы не влезла — минимальный читаемый кегль,
        # честно худший случай, а не тихий отказ чинить вовсе.
        chosen = _MIN_FONT_PT

    if chosen >= current_pt - 0.1:
        return False
    for run in _all_runs(shape):
        run.font.size = Pt(round(chosen, 1))
    return True


def _fix_L03(shape, finding: Finding, profile: TemplateProfile) -> bool:
    return _shrink_text(shape, profile)


def _fix_L04(shape, finding: Finding, profile: TemplateProfile) -> bool:
    return _shrink_text(shape, profile)


# ---------------------------------------------------------------------------
# Починки по типографике/цвету
# ---------------------------------------------------------------------------

def _fix_T01(shape, finding: Finding, profile: TemplateProfile) -> bool:
    family = profile.type_scale.families[0] if profile.type_scale.families else None
    if not family:
        return False
    changed = False
    for run in _all_runs(shape):
        if run.font.name != family:
            run.font.name = family
            changed = True
    return changed


def _fix_T02(shape, finding: Finding, profile: TemplateProfile) -> bool:
    changed = False
    for run in _all_runs(shape):
        size = run.font.size
        if size is None:
            continue
        nearest = _nearest_type_scale_pt(size.pt, profile)
        if abs(nearest - size.pt) > 0.1:
            run.font.size = Pt(round(nearest, 1))
            changed = True
    return changed


def _fix_T03(shape, finding: Finding, profile: TemplateProfile) -> bool:
    changed = False
    for run in _all_runs(shape):
        color = run.font.color
        try:
            if color.type is not None and color.rgb is not None:
                nearest = _nearest_palette_hex(str(color.rgb), profile)
                if nearest.upper() != str(color.rgb).upper():
                    color.rgb = RGBColor.from_string(nearest)
                    changed = True
        except Exception:  # noqa: BLE001 — цвет темы/наследуемый, не прямой RGB
            continue
    try:
        if shape.fill.type is not None and shape.fill.fore_color.type is not None:
            hexv = str(shape.fill.fore_color.rgb)
            nearest = _nearest_palette_hex(hexv, profile)
            if nearest.upper() != hexv.upper():
                shape.fill.fore_color.rgb = RGBColor.from_string(nearest)
                changed = True
    except Exception:  # noqa: BLE001 — заливка темой/градиентом, не прямым RGB
        pass
    return changed


def _fix_T05(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Вернуть логотип/колонтитул на положенное шаблоном место — логотип
    берёт первое известное размещение из каталога ассетов, плейсхолдер
    колонтитула ищется среди плейсхолдеров лейаутов шаблона по типу."""
    cw, ch = profile.canvas_width_emu, profile.canvas_height_emu
    target = None
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE and profile.assets.logo is not None:
        placements = profile.assets.logo.placements or profile.assets.logo_placements
        if placements:
            target = placements[0].box
    elif shape.is_placeholder:
        ph_type = shape.placeholder_format.type
        for entry in profile.layouts:
            for slot in entry.placeholders:
                if ph_type is not None and slot.ph_type.lower() == str(ph_type).split(".")[-1].lower():
                    target = slot.box
                    break
            if target is not None:
                break
    if target is None:
        return False
    shape.left, shape.top = Emu(int(target.left * cw)), Emu(int(target.top * ch))
    return True


def _fix_T06(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Перекрасить текст в чёрный или белый — какой из двух даёт больший
    контраст к фону МАКЕТА под слайдом (`compose.colorpick.slide_background_
    luminance` — та же функция, что уже даёт фон `audit.deterministic`/
    `compose.builder` для той же самой цели, не независимая догадка по
    роли палитры: у этого шаблона `surface`/`background` палитры может быть
    тёмной ролью для карточек, а не фактическим фоном слайда под текстом)."""
    try:
        slide = shape.part.slide
        bg_luminance = slide_background_luminance(slide, profile)
    except Exception:  # noqa: BLE001 — фигура вне обычного дерева слайда (не должно происходить)
        bg_luminance = 1.0
    black_contrast = _contrast(bg_luminance, _relative_luminance("000000"))
    white_contrast = _contrast(bg_luminance, _relative_luminance("FFFFFF"))
    best = "000000" if black_contrast >= white_contrast else "FFFFFF"
    changed = False
    for run in _all_runs(shape):
        try:
            run.font.color.rgb = RGBColor.from_string(best)
            changed = True
        except Exception:  # noqa: BLE001 — цвет темы, не прямой RGB — пропускаем run
            continue
    return changed


def _fix_I05(shape, finding: Finding, profile: TemplateProfile) -> bool:
    """Добавить недостающие подписи осей/легенду графику."""
    if not getattr(shape, "has_chart", False):
        return False
    chart = shape.chart
    changed = False
    if not chart.has_legend:
        chart.has_legend = True
        changed = True
    for axis_name in ("category_axis", "value_axis"):
        axis = getattr(chart, axis_name, None)
        if axis is None:
            continue
        try:
            if not axis.has_title:
                axis.has_title = True
                axis.axis_title.text_frame.text = "Ось" if axis_name == "category_axis" else "Значение"
                changed = True
        except Exception:  # noqa: BLE001 — не все типы графиков несут ось этого рода
            continue
    return changed


_FIXERS = {
    "L01": _fix_L01, "L02": _fix_L02, "L03": _fix_L03, "L04": _fix_L04,
    "L05": _fix_L05, "L06": _fix_L06, "L07": _fix_L07,
    "T01": _fix_T01, "T02": _fix_T02, "T03": _fix_T03, "T05": _fix_T05, "T06": _fix_T06,
    "I05": _fix_I05,
}


def apply_fixes(
    pptx_path: Path, profile: TemplateProfile, findings: list[Finding], finding_ids: dict[str, Finding],
) -> AutofixResult:
    """Применяет починку для каждой находки из `finding_ids`, чей `check_id`
    входит в `SUPPORTED_CHECKS` и у которой есть адресуемый `shape_ref` —
    сохраняет `.pptx` на месте, если хоть одна фигура изменилась.

    `finding_ids` — `{id: Finding}` находок, которые нужно попробовать
    починить (обычно подмножество текущего списка находок варианта,
    выбранное пользователем экрана аудита, либо все автопочинимые сразу —
    см. докстроку `api.jobs` про `autofix=True`)."""
    result = AutofixResult()
    if not finding_ids:
        return result

    prs = Presentation(str(pptx_path))
    for finding_id, finding in finding_ids.items():
        fixer = _FIXERS.get(finding.check_id)
        if fixer is None or finding.slide_index is None or not finding.shape_ref:
            result.skipped.append(finding_id)
            continue
        try:
            slide = prs.slides[finding.slide_index]
        except IndexError:
            result.skipped.append(finding_id)
            continue
        shape = _find_shape(slide, _shape_id_from_ref(finding.shape_ref))
        if shape is None:
            result.skipped.append(finding_id)
            continue
        try:
            did_change = fixer(shape, finding, profile)
        except Exception:  # noqa: BLE001 — эвристика лучше молчаливого отказа не бывает,
            # но и падать на одной находке, оставляя остальные без шанса, тоже нельзя
            did_change = False
        if did_change:
            result.applied.append(finding_id)
            result.changed = True
        else:
            result.skipped.append(finding_id)

    if result.changed:
        prs.save(str(pptx_path))
    return result
