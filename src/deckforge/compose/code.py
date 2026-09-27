"""Фрагмент кода на слайде (задача T2): моногарнитура, строки как есть,
без переносов слов и без подсветки синтаксиса.

Два пути, как у графика. У раскладки со слотом роли `code` (вид `code`,
задача T1) код ложится в фигуру примера: плашку, поля и моногарнитуру там
выбрал дизайнер, меняется только текст и, при нужде, кегль. У раскладки с
крупным текстовым местом код встаёт на это место своей плашкой цвета
палитры шаблона (`muted`/`surface`), моногарнитурой шаблона
(`type_scale.mono`) или системной, и меткой языка мелко.

Кегль подбирается так, чтобы самая длинная строка легла в ширину, а все
строки в высоту: от кегля слота вниз по шкале шаблона до 9 pt, ширина
знака замером моногарнитуры (`textfit.mono_advance`). Перенос внутри
строки кода меняет её смысл на глаз, поэтому он только крайний случай:
строка, не легшая в ширину и на 9 pt, переносится по пробелу с отступом
продолжения (`wrap_code_lines`), а не обрезается краем плашки. Плашка по
высоте кода, а не во всю рамку места.

Фигуры кода называются с префиксом `CODE_SHAPE_NAME`: по нему аудит
узнаёт код и не судит его как текст (стена текста, кегль вне шкалы)."""
from __future__ import annotations
from dataclasses import dataclass

from lxml import etree
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

from deckforge.compose.textfit import mono_advance
from deckforge.compose.colorpick import _relative_luminance, best_contrast_text_color_for_luminance
from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.plan.spec import CodeBlock

# Имя фигур кода: аудит (`audit.deterministic._is_code_item`) узнаёт по
# нему код. Метка языка и плашка идут с тем же префиксом.
CODE_SHAPE_NAME = "DeckForge Code"
# Моногарнитура, если своей у шаблона нет. Consolas стоит в Office на
# Windows и macOS; LibreOffice подменит её своей моно через fontconfig.
FALLBACK_MONO = "Consolas"
# Не мельче 9 pt: мельче код с проектора не читается. Было 10, и строка
# правила на 66 знаков на плашке в полслайда VK Education обрезалась
# краем (рендер 28 сентября 2026), хотя на 9 pt ложилась.
MIN_CODE_PT = 9.0
MAX_CODE_PT = 18.0
# Ширина знака моногарнитуры в долях кегля: у Consolas 0,55, у Menlo и
# Courier New 0,6. Берётся большее, чтобы строка не вылезла за рамку.
MONO_ADVANCE = 0.6
# Интерлиньяж абзаца кода «одинарный», а высота строки в долях кегля
# 1,2: PowerPoint и LibreOffice множат интерлиньяж на высоту строки
# шрифта (у моногарнитур 1,17-1,2 кегля), а не на сам кегль. При
# интерлиньяже 1,15 шесть строк выходили на треть выше замера, и последняя
# вылезала из плашки (рендер 28 сентября 2026).
CODE_LINE_SPACING = 1.0
CODE_LINE_HEIGHT = 1.2
# Отступ строки-продолжения сверх отступа исходной строки, пробелов.
CONTINUATION_INDENT = 4
# Поля плашки вокруг кода, дюймы.
_PAD_IN = 0.15
_EMU_PER_INCH = 914400
_PT_PER_INCH = 72.0
# Плашка отличима от фона, если яркости расходятся хотя бы на столько.
_MIN_PLATE_DELTA = 0.04


def mono_family(profile, slot=None) -> str:
    """Моногарнитура слота кода (`PatternSlot.font_family`, задача T1),
    иначе шаблона (`type_scale.mono`, снята разбором), иначе
    системная."""
    own = getattr(slot, "font_family", None) if getattr(slot, "mono", False) else None
    if own:
        return own
    mono = list(getattr(getattr(profile, "type_scale", None), "mono", None) or [])
    return mono[0] if mono else FALLBACK_MONO


def code_lines(code: CodeBlock) -> list[str]:
    return code.code.splitlines() or [""]


def fits_at(lines: list[str], size_pt: float, width_in: float, height_in: float,
            advance: float = MONO_ADVANCE) -> bool:
    longest = max((len(line) for line in lines), default=0)
    width_ok = longest * advance * size_pt / _PT_PER_INCH <= width_in + 1e-6
    height_ok = len(lines) * size_pt * CODE_LINE_HEIGHT / _PT_PER_INCH <= height_in + 1e-6
    return width_ok and height_ok


@dataclass(frozen=True)
class CodeSize:
    size_pt: float
    fits: bool


def code_size(lines: list[str], width_in: float, height_in: float, start_pt: float, scale: list[float] = (),
              advance: float = MONO_ADVANCE) -> CodeSize:
    """Кегль кода: от `start_pt` (кегль слота, не больше 18) вниз по
    ступеням шкалы шаблона и дальше по целым пунктам до 9 pt, первый, на
    котором код лёг. Не лёг и на 9 pt: 9 pt и `fits=False`."""
    top = max(MIN_CODE_PT, min(MAX_CODE_PT, start_pt or MAX_CODE_PT))
    candidates = {top, MIN_CODE_PT}
    candidates |= {s for s in scale if MIN_CODE_PT <= s <= top}
    candidates |= {float(s) for s in range(int(MIN_CODE_PT), int(top) + 1)}
    for size in sorted(candidates, reverse=True):
        if fits_at(lines, size, width_in, height_in, advance):
            return CodeSize(size, True)
    return CodeSize(MIN_CODE_PT, False)


def wrap_code_lines(lines: list[str], max_chars: int) -> list[str]:
    """Строки длиннее `max_chars` переносятся по последнему пробелу, что
    влезает; продолжение с отступом исходной строки плюс
    `CONTINUATION_INDENT`, чтобы глаз видел, что это та же строка. Без
    пробела в пределах ширины строка рвётся по ширине: обрезанный краем
    плашки код хуже разорванного."""
    out: list[str] = []
    for line in lines:
        if len(line) <= max_chars:
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        cont = " " * (indent + CONTINUATION_INDENT)
        if len(cont) >= max_chars // 2:
            cont = " " * min(indent, max_chars // 4)
        rest = line
        while len(rest) > max_chars:
            lead = len(rest) - len(rest.lstrip(" "))
            cut = rest.rfind(" ", lead + 1, max_chars + 1)
            if cut <= lead:
                piece, tail = rest[:max_chars], rest[max_chars:]
            else:
                piece, tail = rest[:cut].rstrip(), rest[cut + 1:].lstrip(" ")
            out.append(piece)
            rest = cont + tail
        out.append(rest)
    return out


def fit_code_lines(lines: list[str], width_in: float, height_in: float, start_pt: float,
                   scale: list[float] = (), advance: float = MONO_ADVANCE) -> tuple[list[str], CodeSize]:
    """Строки и кегль кода для рамки: кегль по самой длинной строке
    (`code_size`), а если она не легла в ширину и на 9 pt, строки
    переносятся под ширину 9 pt и кегль подбирается заново."""
    size = code_size(lines, width_in, height_in, start_pt, scale, advance)
    if size.fits:
        return lines, size
    max_chars = int((width_in + 1e-6) / (advance * MIN_CODE_PT / _PT_PER_INCH))
    if max_chars < 8:
        return lines, size
    wrapped = wrap_code_lines(lines, max_chars)
    if wrapped == lines:
        return lines, size
    return wrapped, code_size(wrapped, width_in, height_in, start_pt, scale, advance)


# Фигура крупнее этой доли холста считается фоном (картинка-подложка
# лейаута), а не соседом, мешающим расширить рамку кода.
_BACKGROUND_AREA = 0.6
# Пересечение меньше этой доли расширенной рамки не мешает: тонкая линия
# декора у поля или край соседней плашки.
_IGNORABLE_OVERLAP = 0.02


def widen_code_box(slide, box: Box, canvas, grid, lines: list[str], wanted_pt: float) -> Box:
    """Рамка кода шире, если строки не ложатся в неё кеглем `wanted_pt`, а
    справа или слева до поля сетки на той же высоте пусто. Текстовые места
    у раскладок «текст и картинка» в полслайда (VK Education: 4,9″), и
    строка правила на 65 знаков не легла бы туда и на 10 pt, хотя вторая
    половина слайда без нашей картинки пустует."""
    width_in = box.width * canvas.width_in
    height_in = box.height * canvas.height_in
    if fits_at(lines, wanted_pt, width_in - 2 * _PAD_IN, height_in - 2 * _PAD_IN):
        return box
    from deckforge.compose.clone import slide_refs

    others = [
        r.box for r in slide_refs(slide, canvas)
        if r.box is not None and r.box.width * r.box.height < _BACKGROUND_AREA and _overlap(r.box, box) <= 0.0
    ]
    left_edge, right_edge = grid.margin_left, 1 - grid.margin_right
    options = [
        Box(min(box.left, left_edge), box.top, max(box.right, right_edge) - min(box.left, left_edge), box.height),
        Box(box.left, box.top, max(box.right, right_edge) - box.left, box.height),
        Box(min(box.left, left_edge), box.top, box.right - min(box.left, left_edge), box.height),
    ]
    for wide in options:
        if wide.width <= box.width + 1e-6:
            continue
        area = wide.width * wide.height
        if all(_overlap(o, wide) <= _IGNORABLE_OVERLAP * area for o in others):
            return wide
    return box


def _overlap(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.right, b.right) - max(a.left, b.left))
    iy = max(0.0, min(a.bottom, b.bottom) - max(a.top, b.top))
    return ix * iy


def _scale_pt(profile) -> list[float]:
    steps = getattr(getattr(profile, "type_scale", None), "steps", None) or {}
    return [profile.type_scale_pt(name, 0.0) for name in steps]


def _luminance(hex_color: str) -> float:
    return _relative_luminance(hex_color)


def _saturation(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    top = max(r, g, b)
    return 0.0 if top == 0 else (top - min(r, g, b)) / top


def plate_colors(profile, bg_luminance: float) -> tuple[str, str]:
    """(заливка плашки, цвет кода). Плашка из цветов шаблона (роли палитры
    и цвета темы, их же допускает аудит T03): нейтральный цвет, заметно
    отличный от фона под ней и ближайший к нему по яркости, чтобы плашка
    читалась подложкой, а не акцентом. Роли палитры по имени не берутся:
    у VK Education «muted» бывает и фирменным синим, и чёрный код на
    синем не читается. Текст: самый контрастный к плашке цвет палитры."""
    roles = dict(getattr(profile, "palette_roles", None) or {})
    theme = getattr(getattr(profile, "theme", None), "scheme", None) or {}
    pool = list(dict.fromkeys(
        c.upper() for c in [*roles.values(), *theme.values()]
        if isinstance(c, str) and len(c.lstrip("#")) == 6
    ))
    pool = [c if c.startswith("#") else f"#{c}" for c in pool]
    visible = [c for c in pool if abs(_luminance(c) - bg_luminance) >= _MIN_PLATE_DELTA]
    neutral = [c for c in visible if _saturation(c) < 0.25] or visible
    if neutral:
        fill = min(neutral, key=lambda c: abs(_luminance(c) - bg_luminance))
    else:
        fill = "#1E1E1E" if bg_luminance > 0.5 else "#F2F2F2"
    text = best_contrast_text_color_for_luminance(_luminance(fill), roles, preferred_role="on_surface") \
        if roles else ("#FFFFFF" if _luminance(fill) < 0.5 else "#000000")
    return fill, text


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color.lstrip("#").upper())


def _set_no_wrap(tf) -> None:
    tf.word_wrap = False
    body_pr = tf._txBody.find(qn("a:bodyPr"))
    if body_pr is not None:
        body_pr.set("wrap", "none")
        for child in list(body_pr):
            if child.tag in (qn("a:spAutoFit"), qn("a:normAutofit")):
                body_pr.remove(child)
        if body_pr.find(qn("a:noAutofit")) is None:
            etree.SubElement(body_pr, qn("a:noAutofit"))


def _fill_lines(tf, lines: list[str], family: str, size_pt: float, color_hex: str | None) -> None:
    """Строка кода — абзац без маркера: отступы строк сохраняются
    пробелами моногарнитуры, пустая строка остаётся пустым абзацем."""
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.line_spacing = CODE_LINE_SPACING
        p_pr = p._p.get_or_add_pPr()
        for tag in ("a:buChar", "a:buAutoNum", "a:buNone"):
            for el in p_pr.findall(qn(tag)):
                p_pr.remove(el)
        etree.SubElement(p_pr, qn("a:buNone"))
        p_pr.set("indent", "0")
        p_pr.set("marL", "0")
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size_pt)
        run.font.name = family
        run.font.bold = False
        run.font.italic = False
        if color_hex:
            run.font.color.rgb = _rgb(color_hex)


def add_code_block(
    slide, box: Box, code: CodeBlock, profile, bg_luminance: float, *, caption: str | None = None,
) -> list[str]:
    """Код с нуля на рамке `box` (доли холста): плашка, код, метка языка,
    подпись под плашкой. Возвращает находки (не лёг и на 10 pt)."""
    canvas_w, canvas_h = profile.canvas_width_emu, profile.canvas_height_emu
    # Плашка не выходит за холст, даже если место под код заходило за край.
    box = Box(0.0, 0.0, 1.0, 1.0).intersect(box) or box
    left, top = round(box.left * canvas_w), round(box.top * canvas_h)
    width, height = max(1, round(box.width * canvas_w)), max(1, round(box.height * canvas_h))
    findings: list[str] = []

    caption_pt = profile.type_scale_pt("caption", 12.0) or 12.0
    caption_h = 0
    if caption:
        caption_h = round(caption_pt * 1.8 / _PT_PER_INCH * _EMU_PER_INCH)
        if caption_h > height // 3:
            caption_h = 0
    plate_h = height - caption_h

    family = mono_family(profile)
    lines = code_lines(code)
    pad = round(_PAD_IN * _EMU_PER_INCH)
    label_pt = MIN_CODE_PT
    label_h = round(label_pt * 1.6 / _PT_PER_INCH * _EMU_PER_INCH) if code.language else 0
    inner_w = max(1, width - 2 * pad)
    inner_h = max(1, plate_h - 2 * pad - label_h)
    body_pt = profile.type_scale_pt("body", 14.0) or 14.0
    lines, size = fit_code_lines(
        lines, inner_w / _EMU_PER_INCH, inner_h / _EMU_PER_INCH, body_pt, _scale_pt(profile),
        mono_advance(family, MONO_ADVANCE),
    )
    if not size.fits:
        findings.append(
            f"фрагмент кода ({len(lines)} строк, до {max(len(x) for x in lines)} знаков) не лёг в рамку "
            f"и на {MIN_CODE_PT:g} pt"
        )
    # Плашка по высоте кода, а не всей рамки: шесть строк на плашке во всю
    # высоту слайда оставляют под кодом пустой цветной прямоугольник. Так и
    # когда код не лёг: тогда нужная высота больше рамки, и плашка по рамке.
    need_h = round(len(lines) * size.size_pt * CODE_LINE_HEIGHT / _PT_PER_INCH * _EMU_PER_INCH)
    inner_h = min(inner_h, need_h + round(size.size_pt * 0.5 / _PT_PER_INCH * _EMU_PER_INCH))
    plate_h = inner_h + 2 * pad + label_h

    fill, text_color = plate_colors(profile, bg_luminance)
    plate = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(left), Emu(top), Emu(width), Emu(plate_h))
    plate.name = f"{CODE_SHAPE_NAME} plate"
    # Стиль темы у автофигуры несёт контур и тень: у плашки их быть не должно.
    style = plate._element.find(qn("p:style"))
    if style is not None:
        plate._element.remove(style)
    plate.fill.solid()
    plate.fill.fore_color.rgb = _rgb(fill)
    plate.line.fill.background()

    if code.language:
        label = slide.shapes.add_textbox(Emu(left + pad), Emu(top + pad // 2), Emu(inner_w), Emu(label_h))
        label.name = f"{CODE_SHAPE_NAME} language"
        ltf = label.text_frame
        ltf.margin_left = ltf.margin_right = ltf.margin_top = ltf.margin_bottom = Emu(0)
        _set_no_wrap(ltf)
        _fill_lines(ltf, [code.language], family, label_pt, text_color)
        ltf.paragraphs[0].alignment = PP_ALIGN.RIGHT

    textbox = slide.shapes.add_textbox(Emu(left + pad), Emu(top + pad + label_h), Emu(inner_w), Emu(inner_h))
    textbox.name = CODE_SHAPE_NAME
    tf = textbox.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Emu(0)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    _set_no_wrap(tf)
    _fill_lines(tf, lines, family, size.size_pt, text_color)

    if caption and caption_h:
        cap = slide.shapes.add_textbox(Emu(left), Emu(top + plate_h), Emu(width), Emu(caption_h))
        cap.name = "DeckForge Code caption"
        ctf = cap.text_frame
        ctf.margin_left = ctf.margin_right = ctf.margin_bottom = Emu(0)
        ctf.margin_top = Emu(round(caption_pt * 0.3 / _PT_PER_INCH * _EMU_PER_INCH))
        ctf.word_wrap = True
        p = ctf.paragraphs[0]
        run = p.add_run()
        run.text = caption
        run.font.size = Pt(caption_pt)
        families = list(getattr(profile.type_scale, "families", None) or [])
        if families:
            run.font.name = families[0]
        run.font.color.rgb = _rgb(best_contrast_text_color_for_luminance(
            bg_luminance, dict(profile.palette_roles), preferred_role="muted",
        ))
    return findings


def fill_code_shape(element, code: CodeBlock, profile, box_width_in: float, box_height_in: float,
                    start_pt: float) -> list[str]:
    """Код в фигуру примера (слот роли `code` у клона): текст заменяется
    строками кода, гарнитура моно, переносы выключены, кегль от кегля
    слота вниз до 10 pt. Цвет и плашка остаются дизайнерскими."""
    from pptx.text.text import TextFrame

    tx_body = element.find(qn("p:txBody"))
    if tx_body is None:
        return ["у фигуры кода в примере нет текстовой рамки"]
    tf = TextFrame(tx_body, None)
    # Цвет первого прогона примера: код продолжает оформление дизайнера.
    first = tx_body.find(".//" + qn("a:rPr"))
    fill = first.find(qn("a:solidFill")) if first is not None else None
    for p in list(tx_body.findall(qn("a:p")))[1:]:
        tx_body.remove(p)
    first_p = tx_body.find(qn("a:p"))
    if first_p is None:
        first_p = etree.SubElement(tx_body, qn("a:p"))
    for child in list(first_p):
        if child.tag in (qn("a:r"), qn("a:br"), qn("a:fld"), qn("a:endParaRPr")):
            first_p.remove(child)
    _set_no_wrap(tf)
    insets = _insets_in(tx_body)
    width_in = max(0.1, box_width_in - insets[0] - insets[2])
    height_in = max(0.1, box_height_in - insets[1] - insets[3])
    family = mono_family(profile)
    lines, size = fit_code_lines(
        code_lines(code), width_in, height_in, start_pt, _scale_pt(profile), mono_advance(family, MONO_ADVANCE),
    )
    _fill_lines(tf, lines, family, size.size_pt, None)
    if fill is not None:
        for r_pr in tx_body.iter(qn("a:rPr")):
            if r_pr.find(qn("a:solidFill")) is None:
                r_pr.insert(0, etree.fromstring(etree.tostring(fill)))
    nv = element.find(".//" + qn("p:cNvPr"))
    if nv is not None:
        nv.set("name", CODE_SHAPE_NAME)
    if not size.fits:
        return [f"фрагмент кода не лёг в фигуру примера и на {MIN_CODE_PT:g} pt"]
    return []


def _insets_in(tx_body) -> tuple[float, float, float, float]:
    """Поля рамки (лево, верх, право, низ) в дюймах; по умолчанию OOXML."""
    body_pr = tx_body.find(qn("a:bodyPr"))
    defaults = {"lIns": 91440, "tIns": 45720, "rIns": 91440, "bIns": 45720}
    out = []
    for key in ("lIns", "tIns", "rIns", "bIns"):
        raw = body_pr.get(key) if body_pr is not None else None
        out.append((int(raw) if raw is not None else defaults[key]) / _EMU_PER_INCH)
    return tuple(out)
