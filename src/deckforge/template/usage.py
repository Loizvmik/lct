"""Фактическая палитра и шрифты шаблона — по слайдам, лейаутам и мастерам.

Тема (theme.py) даёт номинальную палитру и шрифты бренда, но на реальных
шаблонах Google-экспорта этого мало: соотношение прямых srgbClr к ссылкам
schemeClr на слайдах — от 1074:509 (Education) до 268:570 (WorkSpace), то
есть у части шаблонов прямых цветов на слайдах больше, чем ссылок на тему.
Палитра, собранная только из a:clrScheme, потеряет половину реального
визуального языка. Этот модуль обходит всё дерево шейпов и текста и
считает, что реально нарисовано, а не что заявлено в теме.

Шрифт бренда виден только в a:latin внутри run'ов (fontScheme в теме —
тоже заглушка Google, см. theme.py) — поэтому шрифт каждого run'а читается
напрямую из a:rPr/a:latin, без обращения к lstStyle/txStyles: и то, и
другое либо заглушка, либо требует наследования по уровню/типу плейсхолдера,
которое в масштабе всей задачи не окупается — сигнала без него достаточно.

То же самое ограничение — не только для шрифта: размер (sz), цвет заливки
текста (solidFill), bold/italic читаются исключительно из a:rPr самого
run'а. Run без явного rPr (цвет/размер наследуется от лейаута/мастера)
просто не попадает в соответствующий счётчик — это тихо в смысле "не
кидает предупреждение", но не в смысле "теряет данные молча и делает вид,
что их не было": на трёх реальных шаблонах Google-экспорт почти всегда
пишет свойства явно на run (проверено), но для непроверенного шаблона это
осознанный компромисс, а не незамеченный баг.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field

from deckforge.ooxml.color import Color, UnresolvedColor, resolve_color
from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.ns import local_name, qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import sp_tree_of
from deckforge.template.theme import ThemeInfo

# Единственный активный дочерний элемент заливки по схеме OOXML (CT_FillProperties
# — xs:choice, одновременно их быть не может).
_FILL_TAGS = {"noFill", "solidFill", "gradFill", "grpFill", "pattFill", "blipFill"}

# Токены темы вместо явного имени шрифта. Раскрываем их в major_font/minor_font
# только если тема не деградировавшая — иначе там всё равно Arial-заглушка,
# и раскрытие превратится в шум, а не в сигнал (см. theme.font_scheme_degraded).
_MAJOR_FONT_TOKEN = "+mj-lt"
_MINOR_FONT_TOKEN = "+mn-lt"


@dataclass
class FontUsage:
    family: str
    chars: int = 0
    runs: int = 0
    embedded: bool = False
    bold_available: bool = False
    italic_available: bool = False


@dataclass
class Usage:
    fill: Counter = field(default_factory=Counter)
    text: Counter = field(default_factory=Counter)
    line: Counter = field(default_factory=Counter)
    layout_bg: Counter = field(default_factory=Counter)
    fonts: dict[str, FontUsage] = field(default_factory=dict)
    sizes_pt: Counter = field(default_factory=Counter)
    line_spacing: Counter = field(default_factory=Counter)
    align: Counter = field(default_factory=Counter)
    bold_runs: int = 0
    italic_runs: int = 0
    total_runs: int = 0
    # Не входят в перечень полей брифа — добавлены по прямым требованиям
    # текста Step 4, а не только кода: "noFill учитывается отдельным
    # счётчиком" и общее требование задачи не терять нераспознанные цвета
    # молча (см. докстроку UnresolvedColor в ooxml/color.py).
    no_fill_shapes: int = 0
    unresolved: list[UnresolvedColor] = field(default_factory=list)


def collect_usage(pkg: PptxPackage, canvas: Canvas, theme: ThemeInfo) -> Usage:
    """Обходит все slideLayout*.xml, slideMaster*.xml и slide*.xml пакета."""
    usage = Usage()
    embedded_fonts = _read_embedded_fonts(pkg)

    for part_name in _all_shape_bearing_parts(pkg):
        root = pkg.xml(part_name)
        if part_name.startswith("ppt/slideLayouts/"):
            _collect_layout_bg(root, theme, usage)
        _collect_shapes(root, canvas, theme, usage, embedded_fonts)

    return usage


def _all_shape_bearing_parts(pkg: PptxPackage) -> list[str]:
    prefixes = ("ppt/slideLayouts/slideLayout", "ppt/slideMasters/slideMaster", "ppt/slides/slide")
    return sorted(
        name for name in pkg.names()
        if name.endswith(".xml") and any(name.startswith(p) for p in prefixes)
    )


def _collect_layout_bg(layout_root, theme: ThemeInfo, usage: Usage) -> None:
    c_sld = layout_root.find(qn("p:cSld"))
    bg = c_sld.find(qn("p:bg")) if c_sld is not None else None
    if bg is None:
        return  # фон не задан на уровне лейаута — наследуется, здесь не резолвим

    bg_pr = bg.find(qn("p:bgPr"))
    if bg_pr is not None:
        fill_el = _pick_fill_element(bg_pr)
        if fill_el is None or local_name(fill_el) == "noFill":
            return
        for color in _fill_colors(fill_el, theme, usage.unresolved):
            usage.layout_bg[color] += 1
        return

    bg_ref = bg.find(qn("p:bgRef"))
    if bg_ref is not None:
        color_el = next(iter(bg_ref), None)
        resolved = resolve_color(color_el, theme.scheme, theme.clr_map) if color_el is not None else None
        if isinstance(resolved, Color):
            usage.layout_bg[resolved] += 1
        elif isinstance(resolved, UnresolvedColor):
            usage.unresolved.append(resolved)


def _collect_shapes(
    tree_root, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    # Свой обход, не walk_shapes: тому нужна геометрия (её здесь не считаем),
    # а часть шейпов, важных для палитры/шрифтов (например, плейсхолдеры без
    # своего a:xfrm), в walk_shapes отдаётся с box=None — но это ровно те
    # шейпы, чьи цвет/текст/шрифт нам и нужны, отбрасывать их нельзя.
    sp_tree = sp_tree_of(tree_root)
    for element in _iter_shape_elements(sp_tree):
        tag = local_name(element)
        if tag == "sp":
            _collect_shape_fill(element, theme, usage)
            _collect_line(element, theme, usage)
            _collect_text_body(element, canvas, theme, usage, embedded_fonts)
        elif tag == "cxnSp":
            # Коннектор — линия: у него нет "заливки-карточки" в смысле
            # брифа ("невидимые контейнеры" — про p:sp), noFill здесь почти
            # всегда норма, а не сигнал карточной вёрстки. Обводка (a:ln) —
            # его единственное содержательное визуальное свойство.
            _collect_line(element, theme, usage)


def _iter_shape_elements(container):
    """Рекурсивно отдаёт p:sp/p:cxnSp внутри дерева, разворачивая группы.

    mc:AlternateContent — не ещё один контейнер шейпов, а развилка:
    mc:Choice и mc:Fallback — взаимоисключающие альтернативы ОДНОГО и того
    же содержимого (ECMA-376 Part 3, Markup Compatibility), а не два разных
    шейпа. Слепой `.iter()` по локальным именам спускается в обе ветки и
    удваивает счётчики для любого шейпа, обёрнутого в такую развилку —
    паттерн, которым нативный PowerPoint пользуется регулярно (например,
    для скруглённых прямоугольников с одним настраиваемым радиусом угла),
    в отличие от Google-экспорта, где он не встретился ни разу. Берём
    только одну ветку: mc:Choice, если есть, иначе mc:Fallback — так же,
    как выбрал бы сам PowerPoint при рендере.
    """
    for element in container:
        tag = local_name(element)
        if tag == "AlternateContent":
            branch = _pick_alternate_content_branch(element)
            if branch is not None:
                yield from _iter_shape_elements(branch)
            continue
        if tag in ("sp", "cxnSp"):
            yield element
        elif tag == "grpSp":
            yield from _iter_shape_elements(element)
        # pic/graphicFrame (картинки, таблицы/диаграммы/OLE) не содержат
        # вложенных p:sp/p:cxnSp по схеме — рекурсия в них не нужна, это
        # тот же сознательный вынос таблиц/диаграмм за скобки, что и в
        # остальном модуле (см. докстроку модуля).


def _pick_alternate_content_branch(alt_element):
    choice = next((c for c in alt_element if local_name(c) == "Choice"), None)
    if choice is not None:
        return choice
    return next((c for c in alt_element if local_name(c) == "Fallback"), None)


def _pick_fill_element(container):
    for child in container:
        if local_name(child) in _FILL_TAGS:
            return child
    return None


def _fill_colors(fill_el, theme: ThemeInfo, unresolved: list[UnresolvedColor]) -> list[Color]:
    """Цвет(а) заливки. Для gradFill — цвета всех точек градиента: resolve_color
    сам градиенты не разбирает (см. его докстроку в ooxml/color.py), это
    сделано здесь, отдельно.

    blipFill (заливка изображением) — не «нераспознанный цвет», а легитимное
    отсутствие единого цвета, тот же случай по смыслу, что noFill/grpFill в
    самом resolve_color (там их для этого и заводили). Без явного отсечения
    здесь resolve_color не отличает blipFill от реально неизвестного тега и
    добавляет в unresolved шум "нераспознанный тег" на каждый шейп с
    картинкой-заливкой — подтверждено на контрольном шаблоне защиты."""
    tag = local_name(fill_el)
    if tag == "blipFill":
        return []
    if tag == "gradFill":
        colors: list[Color] = []
        gs_lst = fill_el.find(qn("a:gsLst"))
        if gs_lst is None:
            return colors
        for gs in gs_lst:
            color_el = next(iter(gs), None)
            if color_el is None:
                continue
            resolved = resolve_color(color_el, theme.scheme, theme.clr_map)
            if isinstance(resolved, Color):
                colors.append(resolved)
            elif isinstance(resolved, UnresolvedColor):
                unresolved.append(resolved)
        return colors

    resolved = resolve_color(fill_el, theme.scheme, theme.clr_map)
    if isinstance(resolved, Color):
        return [resolved]
    if isinstance(resolved, UnresolvedColor):
        unresolved.append(resolved)
    return []


def _collect_shape_fill(element, theme: ThemeInfo, usage: Usage) -> None:
    """Заливка и noFill-счётчик — только для p:sp. См. комментарий в
    _collect_shapes про то, почему коннекторы сюда не попадают."""
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return

    fill_el = _pick_fill_element(sp_pr)
    if fill_el is None:
        return  # заливка не задана вовсе — не выдумываем ни цвет, ни noFill
    if local_name(fill_el) == "noFill":
        usage.no_fill_shapes += 1
        return
    for color in _fill_colors(fill_el, theme, usage.unresolved):
        usage.fill[color] += 1


def _collect_line(element, theme: ThemeInfo, usage: Usage) -> None:
    """Обводка — общая для p:sp и p:cxnSp."""
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return
    ln = sp_pr.find(qn("a:ln"))
    if ln is None:
        return
    ln_fill_el = _pick_fill_element(ln)
    if ln_fill_el is None or local_name(ln_fill_el) == "noFill":
        return
    for color in _fill_colors(ln_fill_el, theme, usage.unresolved):
        usage.line[color] += 1


def _collect_text_body(
    sp_element, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return

    for p in tx_body.findall(qn("a:p")):
        _collect_paragraph_props(p, usage)
        for r in p.findall(qn("a:r")):
            usage.total_runs += 1
            _collect_run(r, canvas, theme, usage, embedded_fonts)


def _collect_paragraph_props(p, usage: Usage) -> None:
    p_pr = p.find(qn("a:pPr"))
    if p_pr is None:
        return
    algn = p_pr.get("algn")
    if algn:
        usage.align[algn] += 1
    lnSpc = p_pr.find(qn("a:lnSpc"))
    spc_pct = lnSpc.find(qn("a:spcPct")) if lnSpc is not None else None
    if spc_pct is not None and spc_pct.get("val") is not None:
        usage.line_spacing[int(spc_pct.get("val")) / 1000] += 1


def _collect_run(
    r, canvas: Canvas, theme: ThemeInfo, usage: Usage, embedded_fonts: dict[str, tuple[bool, bool]],
) -> None:
    t_el = r.find(qn("a:t"))
    text = t_el.text or "" if t_el is not None else ""
    chars = len(text)

    r_pr = r.find(qn("a:rPr"))
    if r_pr is None:
        return  # без rPr нечего читать: ни sz, ни latin, ни fill явно не заданы

    if r_pr.get("b") == "1":
        usage.bold_runs += 1
    if r_pr.get("i") == "1":
        usage.italic_runs += 1

    sz_raw = r_pr.get("sz")
    if sz_raw is not None:
        pt = int(sz_raw) / 100 * canvas.norm
        usage.sizes_pt[round(pt * 2) / 2] += 1

    latin_el = r_pr.find(qn("a:latin"))
    family = _resolve_font_family(latin_el, theme) if latin_el is not None else None
    if family:
        fu = usage.fonts.get(family)
        if fu is None:
            bold_av, italic_av = embedded_fonts.get(family, (False, False))
            fu = FontUsage(
                family=family, embedded=family in embedded_fonts,
                bold_available=bold_av, italic_available=italic_av,
            )
            usage.fonts[family] = fu
        fu.chars += chars
        fu.runs += 1

    fill_el = _pick_fill_element(r_pr)
    if fill_el is not None and local_name(fill_el) != "noFill" and chars:
        for color in _fill_colors(fill_el, theme, usage.unresolved):
            usage.text[color] += chars


def _resolve_font_family(latin_el, theme: ThemeInfo) -> str | None:
    typeface = latin_el.get("typeface")
    if not typeface:
        return None
    if typeface in (_MAJOR_FONT_TOKEN, _MINOR_FONT_TOKEN):
        if theme.font_scheme_degraded:
            return None  # тема — заглушка, раскрытие токена дало бы Arial-шум
        return theme.major_font if typeface == _MAJOR_FONT_TOKEN else theme.minor_font
    return typeface


def _read_embedded_fonts(pkg: PptxPackage) -> dict[str, tuple[bool, bool]]:
    """typeface → (доступен ли bold, доступен ли italic) из p:embeddedFontLst
    в presentation.xml. Курсива не бывает ни у одного из трёх шаблонов —
    p:italic там просто нет."""
    root = pkg.xml(pkg.presentation_part())
    lst = root.find(qn("p:embeddedFontLst"))
    if lst is None:
        return {}

    result: dict[str, tuple[bool, bool]] = {}
    for embedded_font in lst.findall(qn("p:embeddedFont")):
        font_el = embedded_font.find(qn("p:font"))
        family = font_el.get("typeface") if font_el is not None else None
        if not family:
            continue
        # p:boldItalic — совмещённый вариант; его наличие тоже означает, что
        # у семейства есть встроенный жирный и встроенный курсивный рисунок,
        # даже если отдельных p:bold/p:italic нет (в трёх шаблонах такого
        # нет, но контракт CT_EmbeddedFontListEntry такое допускает).
        has_bold_italic = embedded_font.find(qn("p:boldItalic")) is not None
        has_bold = embedded_font.find(qn("p:bold")) is not None or has_bold_italic
        has_italic = embedded_font.find(qn("p:italic")) is not None or has_bold_italic
        result[family] = (has_bold, has_italic)
    return result
