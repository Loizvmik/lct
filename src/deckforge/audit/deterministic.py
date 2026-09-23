"""Двадцать четыре детерминированные проверки готовых слайдов (Task 11,
Приложение 1 ТЗ, раздел «Верификация параметров»).

`run_deterministic(pptx_path, profile, config) -> list[Finding]` — вся
работа файла. Всё считается по внутренностям `.pptx` (сырой OOXML через
`deckforge.ooxml` + удобные обёртки `python-pptx` для графиков/таблиц/
картинок), НЕ по растровому рендеру — проверки по изображению (аудит поверх
превью слайда) не входят в границы этой задачи (бриф дословно).

## Почему тут и `deckforge.ooxml.walk_shapes`, и `python-pptx`

Геометрия (`Box`, положение фигуры) обязана учитывать аффинный резолв
ГРУПП — этим уже занимается `walk_shapes`/`geometry.py` (см. их докстроки),
и переизобретать это здесь означало бы повторить ту же работу хуже.
`python-pptx`, в свою очередь, даёт готовый, проверенный разбор графиков
(`Chart.plots`, `.category_axis`, `.has_legend`), таблиц (`Table.rows`/
`.columns`) и картинок (`Picture.image.size` — родные пиксели по байтам
файла, декодированные Pillow) — писать это заново напрямую по XML ради
"только один слой" было бы решением по эстетике, не по существу. Оба слоя
уже сосуществуют в `compose/builder.py` (импортирует и `python-pptx`, и
`deckforge.ooxml`) — тот же прецедент.

Каждая фигура слайда обходится РОВНО один раз (`walk_shapes`, документный
порядок) и сопоставляется с соответствующей обёрткой `python-pptx` по
`p:cNvPr/@id` — единый список `_Item` ниже несёт и геометрию (доли холста,
резолв групп), и содержимое (текст/картинка/таблица/график).

## Детерминизм и порядок

Каждая проверка — чистая функция от байтов файла и порогов `config/
audit.yaml`: не читает время, не использует `random`, не итерируется по
`set`/`dict` в порядке, который мог бы отличаться между запусками (Python
3.7+ гарантирует порядок вставки для `dict`, но не для `set` — там, где
порядок мог бы зависеть от хеша объекта, результат явно сортируется).
Итоговый список сортируется финально ПО СОДЕРЖАНИЮ находки (не по порядку
обхода) — читателю результата (интерфейс подсветки поверх превью) заведомо
удобнее сгруппированный по типу проверки список, и это делает равенство
двух прогонов (ТЗ: "детерминированная проверка... всегда даёт один и тот
же результат") устойчивым к любой случайной перестановки где-либо выше по
стеку, а не только к теоретической.
"""
from __future__ import annotations
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from pptx import Presentation
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE_TYPE

from deckforge.audit.config import AuditConfig
from deckforge.audit.findings import Finding, Severity
from deckforge.compose.colorpick import slide_background_luminance
from deckforge.compose.textfit import measure
from deckforge.ooxml.color import Color, UnresolvedColor, resolve_color
from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import local_name, qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import ShapeRef, walk_shapes
from deckforge.template.naming import contrast_ratio
from deckforge.template.profile import TemplateProfile

EMU_PER_INCH = 914400

CHECK_IDS = (
    "L01", "L02", "L03", "L04", "L05", "L06", "L07",
    "T01", "T02", "T03", "T04", "T05", "T06",
    "D01", "D02", "D03", "D04", "D05",
    "I01", "I02", "I03", "I04", "I05", "I06",
)

# Тот же дефолт интерлиньяжа, что и `textfit._DEFAULT_LINE_SPACING` —
# используется здесь ТОЛЬКО когда у абзаца нет явного `a:lnSpc` (аудит
# читает произвольный файл, не обязательно собранный `compose/builder.py`,
# который явно передаёт интерлиньяж шаблона).
_DEFAULT_LINE_SPACING = 1.2

_PIE_LIKE = frozenset({
    XL_CHART_TYPE.PIE, XL_CHART_TYPE.PIE_EXPLODED, XL_CHART_TYPE.PIE_OF_PIE,
    XL_CHART_TYPE.DOUGHNUT, XL_CHART_TYPE.DOUGHNUT_EXPLODED, XL_CHART_TYPE.BAR_OF_PIE,
})


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------


def run_deterministic(pptx_path: Path, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    """Все 24 проверки на `pptx_path`. I01 ("файл не открывается") —
    единственная, что может оборвать разбор остальных: если файл не
    открывается ни `zipfile`, ни `python-pptx`, дальше анализировать
    нечего, и результат — ровно один finding."""
    pptx_path = Path(pptx_path)
    opened = _open(pptx_path)
    if isinstance(opened, Finding):
        return [opened]
    pkg, prs, canvas = opened

    findings: list[Finding] = []
    slide_texts: list[str] = []
    for index, slide in enumerate(prs.slides):
        ctx = _build_context(index, slide, prs, pkg, canvas, profile)
        findings.extend(_check_slide(ctx, profile, config))
        slide_texts.append(ctx.full_text)

    findings.extend(_check_I06(slide_texts, config))

    return _stable_sort(findings)


# ---------------------------------------------------------------------------
# Аудит ОДНОГО уже уложенного слайда, ДО сохранения файла (Task 13
# продолжение брифа: "Аудит — часть пайплайна, а не внешняя проверка" —
# `compose.builder` зовёт это ПОСЛЕ укладки каждого кандидата раскладки,
# чтобы решить, брать эту раскладку или пробовать следующую).
#
# Пять проверок, не все 24 — буквальный список брифа ("наложение, выход за
# границы, невлезающий текст, заполненность вне допуска"): L01 (за
# границами), L02 (наложение), L03/L04 (текст не помещается/обрезан
# краем), D05 (заполненность вне допуска). T01-T06/D01-D04/I02-I05 — либо
# свойство ШАБЛОНА/СОДЕРЖАНИЯ, а не конкретной геометрии кандидата (шрифт,
# цвет, число буллетов не меняется от того, какую раскладку из одного и
# того же `kind` выбрали), либо требуют ВСЕЙ колоды целиком (I06 сравнивает
# заголовки МЕЖДУ слайдами) — гонять их на каждой из 2-3 попыток каждого
# слайда было бы работой, которую негде использовать: решение "какую
# раскладку взять" эти пять не то что не улучшат — не могут отличить один
# кандидат этого же `kind` от другого. Полный прогон всех 24 остаётся один
# раз на готовую колоду (`_cmd_generate`/`run_deterministic`), не дублируется.
_LAYOUT_ERROR_CHECK_IDS = frozenset({"L01", "L02", "L03", "L04", "D05"})


def audit_slide_layout(
    slide, canvas: Canvas, profile: TemplateProfile, config: AuditConfig, *, index: int = 0,
) -> list[Finding]:
    """`_LAYOUT_ERROR_CHECK_IDS`-проверки одного УЖЕ УЛОЖЕННОГО python-pptx
    `slide` — БЕЗ сохранения на диск и повторного открытия: `slide._element`
    (python-pptx строит свои oxml-элементы поверх `lxml.etree`, тот же
    `walk_shapes`/`qn()` работает с ним один в один, как с XML, распарсенным
    из файла заново — проверено чтением исходника `pptx.oxml`) даёт корень
    дерева шейпов напрямую. Save+reopen через `zipfile`/`PptxPackage` на
    КАЖДУЮ из 2-3 проверяемых раскладок КАЖДОГО слайда колоды обошёлся бы
    дороже, чем сама проверка (полный прогон 24 проверок на готовую колоду
    из 12-15 слайдов — доли секунды, см. отчёт задачи; здесь проверяется
    один слайд пятью проверками в разы легче)."""
    ctx = _build_context_inmemory(index, slide, canvas, profile)
    findings: list[Finding] = []
    findings.extend(_check_L01(ctx, config))
    findings.extend(_check_L02(ctx, config))
    findings.extend(_check_L03(ctx, config))
    findings.extend(_check_L04(ctx, config))
    findings.extend(_check_D05(ctx, config))
    return _stable_sort(findings)


# ---------------------------------------------------------------------------
# Открытие файла — I01
# ---------------------------------------------------------------------------


def _open(pptx_path: Path):
    try:
        with zipfile.ZipFile(pptx_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise zipfile.BadZipFile(f"повреждён элемент архива: {bad}")
        pkg = PptxPackage.open(pptx_path)
        canvas = pkg.canvas()
        prs = Presentation(str(pptx_path))
        # `python-pptx` не всегда падает сразу на структурно сломанном
        # пакете — трогаем `.slides` явно, чтобы разбор дерева слайдов
        # произошёл здесь, а не посреди первой проверки.
        _ = len(prs.slides)
    except Exception as exc:  # noqa: BLE001 — любая причина здесь означает одно: файл не открылся
        return Finding(
            check_id="I01", severity="critical", slide_index=None, shape_ref=None,
            message=f"Файл не открывается: {exc}", box=None,
            fixable=False, fix_hint="Файл повреждён — пересобрать колоду заново.",
        )
    return pkg, prs, canvas


# ---------------------------------------------------------------------------
# Контекст одного слайда
# ---------------------------------------------------------------------------


@dataclass
class _Item:
    shape_id: str
    name: str
    kind: str  # "shape" | "picture" | "graphic_frame" | "connector"
    element: object
    box: Box
    text: str = ""
    has_fill: bool = False
    fill_color: Color | UnresolvedColor | None = None
    pptx_shape: object | None = None
    is_table: bool = False
    is_chart: bool = False


@dataclass
class _SlideContext:
    index: int
    slide: object
    canvas: Canvas
    items: list[_Item]
    layout_part_name: str
    layout_entry: object | None
    bg_luminance: float
    bg_hex: str | None
    scheme: dict
    clr_map: dict
    full_text: str = ""


def _build_context(index: int, slide, prs, pkg: PptxPackage, canvas: Canvas, profile: TemplateProfile) -> _SlideContext:
    slide_part = str(slide.part.partname).lstrip("/")
    root = pkg.xml(slide_part)
    return _context_from_root(index, slide, root, canvas, profile)


def _build_context_inmemory(index: int, slide, canvas: Canvas, profile: TemplateProfile) -> _SlideContext:
    """Тот же `_build_context`, но БЕЗ файла на диске и `PptxPackage` —
    `root` берётся напрямую из уже открытого python-pptx `slide`
    (`slide._element`, см. докстроку `audit_slide_layout` про то, почему
    это тот же самый lxml-элемент, что дал бы файловый путь). Используется
    ТОЛЬКО `audit_slide_layout` (аудит одного слайда внутри цикла сборки,
    Task 13 продолжение)."""
    return _context_from_root(index, slide, slide._element, canvas, profile)  # noqa: SLF001 — тот же приём, что и `compose.builder._clear_sample_slides`/`_remove_last_slide` (см. их докстроки)


def _context_from_root(index: int, slide, root, canvas: Canvas, profile: TemplateProfile) -> _SlideContext:
    id_to_pptx = {}
    for shape in _flatten_pptx_shapes(slide.shapes):
        try:
            id_to_pptx[str(shape.shape_id)] = shape
        except Exception:  # noqa: BLE001 — фигура без валидного id не адресуема, пропускаем
            continue

    scheme = dict(profile.theme.scheme)
    clr_map = dict(profile.theme.clr_map)

    items: list[_Item] = []
    for ref in walk_shapes(root, canvas, include_groups=False):
        if ref.box is None or ref.kind == "group":
            continue
        item = _make_item(ref, id_to_pptx.get(ref.shape_id), scheme, clr_map)
        items.append(item)

    layout_part_name = str(slide.slide_layout.part.partname).lstrip("/")
    # T04 обязана отличать "слайд правда собран на макете ЭТОГО шаблона" от
    # случайного совпадения ИМЕНИ ПАРТА: `ppt/slideLayouts/slideLayoutN.xml`
    # — позиционная нумерация OOXML, не глобальный идентификатор, и у
    # СОВЕРШЕННО постороннего файла (в т.ч. стокового `python-pptx`) part
    # с тем же именем находится почти всегда (разведано: `slideLayout7.xml`
    # есть и в пустой презентации python-pptx по умолчанию, и в VK Tech —
    # под разными макетами). Совпадения part_name недостаточно — требуется
    # ЕЩЁ совпадение имени макета (`p:cSld/@name`) И холста колоды с
    # холстом шаблона (`profile.canvas_*`): у постороннего файла хотя бы
    # одно из трёх почти всегда разойдётся, у колоды, реально собранной
    # `compose.builder.build_deck` (тот же файл шаблона как база) — все три
    # совпадают по построению.
    layout_entry = None
    if profile.canvas_width_emu == canvas.width_emu and profile.canvas_height_emu == canvas.height_emu:
        layout_entry = next(
            (e for e in profile.layouts if e.part_name == layout_part_name and e.name == slide.slide_layout.name),
            None,
        )

    bg_luminance = slide_background_luminance(slide, profile)
    bg_hex = _slide_own_background_hex(root, scheme, clr_map)
    if bg_hex is None and layout_entry is not None and layout_entry.background.color is not None:
        color = layout_entry.background.color
        if color.resolved and color.hex:
            bg_hex = color.hex

    full_text = "\n".join(item.text for item in items if item.text)

    return _SlideContext(
        index=index, slide=slide, canvas=canvas, items=items,
        layout_part_name=layout_part_name, layout_entry=layout_entry,
        bg_luminance=bg_luminance, bg_hex=bg_hex, scheme=scheme, clr_map=clr_map,
        full_text=full_text,
    )


def _flatten_pptx_shapes(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _flatten_pptx_shapes(shape.shapes)


def _make_item(ref: ShapeRef, pptx_shape, scheme: dict, clr_map: dict) -> _Item:
    text = ""
    has_fill = False
    fill_color: Color | UnresolvedColor | None = None
    is_table = False
    is_chart = False

    if ref.kind == "shape":
        tx_body = ref.element.find(qn("p:txBody"))
        if tx_body is not None:
            text = _joined_text(tx_body)
        sp_pr = ref.element.find(qn("p:spPr"))
        fill_node = _find_fill_node(sp_pr) if sp_pr is not None else None
        if fill_node is not None:
            fill_color = resolve_color(fill_node, scheme, clr_map)
            has_fill = isinstance(fill_color, Color)
    elif ref.kind == "graphic_frame":
        if pptx_shape is not None:
            try:
                is_table = bool(pptx_shape.has_table)
            except Exception:  # noqa: BLE001
                is_table = False
            try:
                is_chart = bool(pptx_shape.has_chart)
            except Exception:  # noqa: BLE001
                is_chart = False
        if is_table:
            text = _table_text(pptx_shape.table)

    return _Item(
        shape_id=ref.shape_id, name=ref.name, kind=ref.kind, element=ref.element, box=ref.box,
        text=text, has_fill=has_fill, fill_color=fill_color, pptx_shape=pptx_shape,
        is_table=is_table, is_chart=is_chart,
    )


_FILL_TAGS = ("a:noFill", "a:solidFill", "a:gradFill", "a:grpFill")


def _find_fill_node(container):
    for tag in _FILL_TAGS:
        el = container.find(qn(tag))
        if el is not None:
            return el
    return None


def _joined_text(tx_body) -> str:
    paragraphs = tx_body.findall(qn("a:p"))
    return "\n".join(_paragraph_text(p) for p in paragraphs)


def _paragraph_text(p_el) -> str:
    parts = []
    for child in p_el:
        tag = local_name(child)
        if tag == "r":
            t_el = child.find(qn("a:t"))
            parts.append(t_el.text or "" if t_el is not None else "")
        elif tag == "br":
            parts.append("\n")
    return "".join(parts)


def _table_text(table) -> str:
    lines = []
    for row in table.rows:
        for cell in row.cells:
            if cell.text_frame.text.strip():
                lines.append(cell.text_frame.text)
    return "\n".join(lines)


def _slide_own_background_hex(slide_root, scheme: dict, clr_map: dict) -> str | None:
    """Явный `p:bg` НА САМОМ слайде (не унаследованный от макета) — если он
    сплошная заливка, резолвим её; градиент/картинка/отсутствие `p:bg`
    (наследование от макета) — `None`, фон берётся из `layout_entry`.
    `slide_root` — всегда `p:sld` (см. `_build_context`)."""
    if local_name(slide_root) != "sld":
        return None
    c_sld = slide_root.find(qn("p:cSld"))
    bg = c_sld.find(qn("p:bg")) if c_sld is not None else None
    if bg is None:
        return None
    bg_pr = bg.find(qn("p:bgPr"))
    if bg_pr is None:
        return None
    fill_node = _find_fill_node(bg_pr)
    if fill_node is None or local_name(fill_node) != "solidFill":
        return None
    color = resolve_color(fill_node, scheme, clr_map)
    return color.hex if isinstance(color, Color) else None


# ---------------------------------------------------------------------------
# WCAG-контраст — та же формула, что уже трижды посчитана в проекте
# (`template/naming.py::_relative_luminance`, `compose/colorpick.py`,
# `compose/builder.py`) — здесь четвёртый раз, тем же намеренным решением:
# три модуля-потребителя уже не делят один код ради независимости друг от
# друга (см. докстроку `colorpick.py`), аудит — такой же независимый
# четвёртый потребитель одной и той же публичной формулы WCAG, не частная
# бизнес-логика, которая рискует разъехаться по смыслу.
# ---------------------------------------------------------------------------


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast_from_luminance(l_a: float, l_b: float) -> float:
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Диспетчер проверок одного слайда
# ---------------------------------------------------------------------------


def _check_slide(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_check_L01(ctx, config))
    findings.extend(_check_L02(ctx, config))
    findings.extend(_check_L03(ctx, config))
    findings.extend(_check_L04(ctx, config))
    findings.extend(_check_L05(ctx, profile, config))
    findings.extend(_check_L06(ctx, profile, config))
    findings.extend(_check_L07(ctx, config))
    findings.extend(_check_T01(ctx, profile, config))
    findings.extend(_check_T02(ctx, profile, config))
    findings.extend(_check_T03(ctx, profile, config))
    findings.extend(_check_T04(ctx, profile))
    findings.extend(_check_T05(ctx, profile, config))
    findings.extend(_check_T06(ctx, config))
    findings.extend(_check_D01(ctx, config))
    findings.extend(_check_D02(ctx, config))
    findings.extend(_check_D03(ctx, config))
    findings.extend(_check_D04(ctx, config))
    findings.extend(_check_D05(ctx, config))
    findings.extend(_check_I02(ctx, config))
    findings.extend(_check_I03(ctx, profile, config))
    findings.extend(_check_I04(ctx, config))
    findings.extend(_check_I05(ctx))
    return findings


def _finding(
    check_id: str, severity: Severity, ctx: _SlideContext | None, item: _Item | None,
    message: str, box: Box | None, fixable: bool, fix_hint: str,
) -> Finding:
    return Finding(
        check_id=check_id, severity=severity,
        slide_index=ctx.index if ctx is not None else None,
        shape_ref=(f"{item.shape_id}:{item.name}" if item is not None else None),
        message=message, box=box, fixable=fixable, fix_hint=fix_hint,
    )


# ---------------------------------------------------------------------------
# L01 — элемент вышел за границы слайда
# ---------------------------------------------------------------------------

_BOUNDS_EPS = 1e-6


def _check_L01(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    findings = []
    for item in ctx.items:
        b = item.box
        if (
            b.left < -_BOUNDS_EPS or b.top < -_BOUNDS_EPS
            or b.right > 1 + _BOUNDS_EPS or b.bottom > 1 + _BOUNDS_EPS
        ):
            findings.append(_finding(
                "L01", "critical", ctx, item,
                f"Фигура «{item.name or item.shape_id}» выходит за границы слайда "
                f"(рамка [{b.left:.3f}, {b.top:.3f}, {b.right:.3f}, {b.bottom:.3f}] в долях холста).",
                b, True, "Переместить или уменьшить фигуру так, чтобы она целиком лежала на холсте.",
            ))
    return findings


# ---------------------------------------------------------------------------
# Внутренние поля текстовой рамки (`a:bodyPr` lIns/tIns/rIns/bIns)
# ---------------------------------------------------------------------------

# Дефолты ECMA-376 Part 1, §21.1.2.1.1 (CT_TextBodyProperties) для
# lIns/rIns/tIns/bIns, когда атрибут в разметке отсутствует — 0.1″/0.1″/
# 0.05″/0.05″ (в EMU). НАША сборка обнуляет их явно (`compose/builder.py::
# _draw_slot` — единственная причина, по которой `measure()` там мерит по
# ПОЛНОЙ ширине/высоте фигуры и это корректно), но аудит применяется к
# ПРОИЗВОЛЬНОМУ чужому файлу — там поля почти наверняка НЕ нулевые (Task 11
# повторное ревью, находка №2: `python-pptx` без явного `tf.margin_*`
# оставляет их на этом самом дефолте). Без вычитания полей замер видит
# больше места, чем реально доступно тексту, и "не влезает"/"обрезан
# краем" (L03/L04) и заполненность (L02/D05, через `_effective_box`)
# молчат там, где реально должны сработать.
_DEFAULT_LINS_EMU = 91440
_DEFAULT_TINS_EMU = 45720
_DEFAULT_RINS_EMU = 91440
_DEFAULT_BINS_EMU = 45720


def _text_frame_insets_in(sp_element) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) внутренних полей `a:bodyPr` в дюймах —
    ЯВНОЕ значение атрибута, если задано, иначе дефолт спецификации (см.
    докстроку выше). Отсутствие `p:txBody`/`a:bodyPr` — тот же дефолт: текст
    без явной рамки всё равно рисуется с дефолтными полями, это не "нулевые
    поля" по умолчанию."""
    tx_body = sp_element.find(qn("p:txBody"))
    body_pr = tx_body.find(qn("a:bodyPr")) if tx_body is not None else None

    def _inset(attr: str, default_emu: int) -> float:
        raw = body_pr.get(attr) if body_pr is not None else None
        if raw is None:
            return default_emu / EMU_PER_INCH
        try:
            return int(raw) / EMU_PER_INCH
        except ValueError:
            return default_emu / EMU_PER_INCH

    return (
        _inset("lIns", _DEFAULT_LINS_EMU), _inset("tIns", _DEFAULT_TINS_EMU),
        _inset("rIns", _DEFAULT_RINS_EMU), _inset("bIns", _DEFAULT_BINS_EMU),
    )


# ---------------------------------------------------------------------------
# L02 — два блока наложились друг на друга
# ---------------------------------------------------------------------------


def _effective_box(item: _Item, canvas: Canvas) -> Box:
    """Площадь текстового блока — ИЗМЕРЕННАЯ (`textfit.measure`), а не
    объявленная: сборка нарочно даёт блокам запас по высоте (см. докстроку
    модуля/бриф), и объявленная рамка дала бы ложные наложения. Высота
    ограничена сверху объявленной (текст, переполнивший рамку, — отдельная
    находка L03/L04, не повод удвоить площадь для L02).

    Правка по итогам обязательного осмотра (отчёт задачи): усадка НЕ
    применяется, если у фигуры есть СОБСТВЕННАЯ непрозрачная заливка
    (`item.has_fill`) — плашка/карточка (`compose/diagrams.py::_add_card`
    и подобные) даёт видимые чернила на ВСЮ свою рамку целиком, независимо
    от того, сколько места внутри реально занимает текст (карточка схемы
    "Заявка" на крошечной подписи посреди большого чёрного прямоугольника
    — прямоугольник виден целиком, а не только текст в нём). Усадка имеет
    смысл только для ГОЛОГО текста без своей заливки: там объявленная
    рамка — запас на переполнение, а не видимая площадь. Без этой поправки
    D05 на реальной demo-колоде (Task 11, обязательный осмотр) занижал
    заполненность слайда со схемой "process" вчетверо (11% вместо
    фактических ~37% чёрных карточек) и грозил ложным "слайд почти пуст"
    там, где на рендере холст занят заметно.

    Правка Task 11 повторного ревью (находка №2): ширина/высота, которыми
    мерится текст, теперь за вычетом внутренних полей рамки (`a:bodyPr`,
    см. `_text_frame_insets_in`) — на нашей сборке поля нулевые, ничего не
    меняется, на чужом файле с полями по умолчанию текст меряется по
    реально доступному месту, не по полной рамке. Эффективная высота
    после капа ДОБАВЛЯЕТ поля обратно (контент-высота — это не вся видимая
    площадь блока, вокруг неё ещё есть поля) — так на нулевых полях
    поведение бит-в-бит прежнее."""
    if item.kind != "shape" or not item.text.strip() or item.has_fill:
        return item.box
    style = _dominant_run_style(item.element)
    if style is None:
        return item.box
    family, size_pt, _bold = style
    l_in, t_in, r_in, b_in = _text_frame_insets_in(item.element)
    box_width_in = max(0.0, item.box.width * canvas.width_in - l_in - r_in)
    line_spacing = _first_paragraph_line_spacing(item.element)
    metrics = measure(item.text, family, size_pt, box_width_in, line_spacing=line_spacing)
    declared_full_height_in = item.box.height * canvas.height_in
    available_height_in = max(0.0, declared_full_height_in - t_in - b_in)
    effective_content_height_in = min(metrics.height_in, available_height_in)
    effective_height_in = min(effective_content_height_in + t_in + b_in, declared_full_height_in)
    effective_height = effective_height_in / canvas.height_in if canvas.height_in else item.box.height
    return Box(left=item.box.left, top=item.box.top, width=item.box.width, height=effective_height)


def _overlap_ratio(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.right, b.right) - max(a.left, b.left))
    iy = max(0.0, min(a.bottom, b.bottom) - max(a.top, b.top))
    inter = ix * iy
    if inter <= 0.0:
        return 0.0
    return inter / min(a.width * a.height, b.width * b.height)


def _fully_contains(outer: Box, inner: Box, tolerance: float) -> bool:
    return (
        outer.left - tolerance <= inner.left and outer.top - tolerance <= inner.top
        and inner.right <= outer.right + tolerance and inner.bottom <= outer.bottom + tolerance
    )


def _is_plate(item: _Item) -> bool:
    """Автошейп без текста с непрозрачной заливкой — визуальная подложка,
    не самостоятельный контент; полное вложение содержимого в такую фигуру
    — намеренная композиция (карточка на своей плашке), не брак."""
    return item.kind == "shape" and not item.text.strip() and item.has_fill


def _is_background(item: _Item) -> bool:
    """`_is_plate` ПЛЮС картинка — та же логика "подложка, не отдельный
    контент", распространённая на фотографии/скриншоты-фоны. Находка
    ручного осмотра demo-колоды (см. отчёт задачи): бейдж-иконка (маленький
    автошейп) и декоративное фоновое фото шаблона (лейаут "1_Содержание",
    слайд, скопированный из самого шаблона) — бейдж ПОЛНОСТЬЮ лежит внутри
    фото, это намеренная композиция дизайнера ("иконка поверх фонового
    снимка"), не брак. Диаграмма/таблица (`kind="graphic_frame"`) НЕ входит
    сюда намеренно — полное вложение текста в график остаётся браком (бриф:
    "текст на плашке — не наложение, текст на графике — наложение")."""
    return _is_plate(item) or item.kind == "picture"


def _check_L02(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    cfg = config.layout
    blocks = [
        (item, _effective_box(item, ctx.canvas))
        for item in ctx.items
        if item.kind != "connector" and (item.text.strip() or item.has_fill or item.kind in ("picture", "graphic_frame"))
    ]
    canvas_area_in2 = ctx.canvas.width_in * ctx.canvas.height_in
    findings = []
    seen: set[tuple[str, str]] = set()
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            item_a, box_a = blocks[i]
            item_b, box_b = blocks[j]
            # Полное вложение в декоративную плашку (автошейп без текста со
            # своей заливкой) — легитимная композиция "текст на своей
            # карточке", не наложение (см. `_is_plate`). Полное вложение в
            # НЕ-плашку (например, текст поверх графика/картинки) — тот же
            # брак, что и частичное наложение, не исключение: контент
            # заслоняет контент независимо от того, торчит ли он за края.
            if _is_background(item_a) and _fully_contains(box_a, box_b, cfg.margin_tolerance):
                continue
            if _is_background(item_b) and _fully_contains(box_b, box_a, cfg.margin_tolerance):
                continue
            ratio = _overlap_ratio(box_a, box_b)
            if ratio <= cfg.overlap_ratio:
                continue
            ix = max(0.0, min(box_a.right, box_b.right) - max(box_a.left, box_b.left))
            iy = max(0.0, min(box_a.bottom, box_b.bottom) - max(box_a.top, box_b.top))
            area_in2 = ix * iy * canvas_area_in2
            if area_in2 < cfg.overlap_min_area_in2:
                continue
            key = tuple(sorted((item_a.shape_id, item_b.shape_id)))
            if key in seen:
                continue
            seen.add(key)
            union_box = Box(
                left=min(box_a.left, box_b.left), top=min(box_a.top, box_b.top),
                width=max(box_a.right, box_b.right) - min(box_a.left, box_b.left),
                height=max(box_a.bottom, box_b.bottom) - min(box_a.top, box_b.top),
            )
            findings.append(_finding(
                "L02", "major", ctx, item_a,
                f"Блоки «{item_a.name or item_a.shape_id}» и «{item_b.name or item_b.shape_id}» "
                f"наложились друг на друга ({ratio:.0%} площади меньшего блока).",
                union_box, True, "Раздвинуть блоки или уменьшить один из них так, чтобы наложение исчезло.",
            ))
    return findings


# ---------------------------------------------------------------------------
# L03 — текст не поместился в свою рамку / L04 — обрезан краем слайда
# ---------------------------------------------------------------------------


def _dominant_run_style(sp_element) -> tuple[str, float, bool] | None:
    """(гарнитура, кегль pt, полужирность) самого "весомого" run'а шейпа —
    голосование по числу символов, тот же приём, что и `template/patterns.
    py::_shape_dominant_size` (см. его докстроку)."""
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return None
    votes: dict[tuple[str, float, bool], int] = {}
    for r in tx_body.iter(qn("a:r")):
        r_pr = r.find(qn("a:rPr"))
        if r_pr is None:
            continue
        sz_raw = r_pr.get("sz")
        latin = r_pr.find(qn("a:latin"))
        family = latin.get("typeface") if latin is not None else None
        if sz_raw is None or not family:
            continue
        size_pt = int(sz_raw) / 100
        bold = r_pr.get("b") == "1"
        t_el = r.find(qn("a:t"))
        chars = len(t_el.text or "") if t_el is not None else 0
        key = (family, size_pt, bold)
        votes[key] = votes.get(key, 0) + max(chars, 1)
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def _first_paragraph_line_spacing(sp_element) -> float:
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return _DEFAULT_LINE_SPACING
    p_el = tx_body.find(qn("a:p"))
    if p_el is None:
        return _DEFAULT_LINE_SPACING
    p_pr = p_el.find(qn("a:pPr"))
    if p_pr is None:
        return _DEFAULT_LINE_SPACING
    ln_spc = p_pr.find(qn("a:lnSpc"))
    if ln_spc is None:
        return _DEFAULT_LINE_SPACING
    pct = ln_spc.find(qn("a:spcPct"))
    if pct is None or pct.get("val") is None:
        return _DEFAULT_LINE_SPACING
    return int(pct.get("val")) / 100000.0


def _check_L03(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    findings = []
    for item in ctx.items:
        if item.kind != "shape" or not item.text.strip():
            continue
        style = _dominant_run_style(item.element)
        if style is None:
            continue
        family, size_pt, _bold = style
        l_in, t_in, r_in, b_in = _text_frame_insets_in(item.element)
        box_width_in = max(0.0, item.box.width * ctx.canvas.width_in - l_in - r_in)
        line_spacing = _first_paragraph_line_spacing(item.element)
        metrics = measure(item.text, family, size_pt, box_width_in, line_spacing=line_spacing)
        declared_height_in = max(0.0, item.box.height * ctx.canvas.height_in - t_in - b_in)
        if metrics.height_in > declared_height_in + config.layout.text_fit_tolerance_in:
            findings.append(_finding(
                "L03", "major", ctx, item,
                f"Текст «{item.name or item.shape_id}» не помещается в свою рамку "
                f"(нужно {metrics.height_in:.2f}″, доступно {declared_height_in:.2f}″"
                f"{' за вычетом внутренних полей рамки' if (t_in or b_in) else ''}).",
                item.box, True, "Уменьшить кегль по шкале шаблона или увеличить рамку слота.",
            ))
    return findings


def _check_L04(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    findings = []
    for item in ctx.items:
        if item.kind != "shape" or not item.text.strip():
            continue
        # Уже пойман L01 (сама рамка фигуры вне холста) — L04 про другой
        # симптом (текст ВЫЛИВАЕТСЯ за край при рамке, лежащей в холсте),
        # не дублируем находку по той же фигуре.
        if item.box.right > 1 + _BOUNDS_EPS or item.box.bottom > 1 + _BOUNDS_EPS or item.box.left < -_BOUNDS_EPS or item.box.top < -_BOUNDS_EPS:
            continue
        style = _dominant_run_style(item.element)
        if style is None:
            continue
        family, size_pt, _bold = style
        l_in, t_in, r_in, b_in = _text_frame_insets_in(item.element)
        box_width_in = max(0.0, item.box.width * ctx.canvas.width_in - l_in - r_in)
        line_spacing = _first_paragraph_line_spacing(item.element)
        metrics = measure(item.text, family, size_pt, box_width_in, line_spacing=line_spacing)
        # Полная вертикальная протяжённость содержимого — поля рамки ПЛЮС
        # измеренный текст (см. `_text_frame_insets_in`/докстроку
        # `_effective_box`) — сравнивается с объявленной высотой рамки, берём
        # большее (та же логика, что была, теперь с учётом полей).
        content_height_in = t_in + metrics.height_in + b_in
        effective_height_in = max(content_height_in, item.box.height * ctx.canvas.height_in)
        effective_bottom = item.box.top + effective_height_in / ctx.canvas.height_in
        if effective_bottom > 1 + _BOUNDS_EPS:
            findings.append(_finding(
                "L04", "critical", ctx, item,
                f"Текст «{item.name or item.shape_id}» переполняет рамку настолько, что "
                "выходит за нижний край слайда.",
                Box(item.box.left, item.box.top, item.box.width, min(1.0, effective_bottom) - item.box.top),
                True, "Уменьшить кегль/сократить текст или сдвинуть блок от края.",
            ))
    return findings


# ---------------------------------------------------------------------------
# L05 — блоки не выровнены по направляющим макета
# ---------------------------------------------------------------------------

# Порог "достойная ось" для L05 — доля ВСЕХ left-измерений шаблона,
# поддержавших колонную ось (`ColumnAxis.confidence`, см. её докстроку в
# `template/grid.py`).
#
# Найдено повторным код-ревью (Task 11): раньше L05 брал не "достойные" оси,
# а первые четыре ПО СПИСКУ `Grid.columns` (тот отсортирован по confidence)
# — и явные направляющие (`p:guide`) занимали все четыре места чужой
# уверенностью, форсированной в 1.0 независимо от реальной поддержки (см.
# исправленную докстроку `ColumnAxis` в grid.py), выталкивая настоящие
# кластерные колонны с поддержкой на порядки больше. Правильный критерий —
# содержательный, не позиционный: ось ДОСТОЙНА направляющей для L05, если
# она либо явная направляющая из файла (`source == "guide"` — её поставил
# дизайнер, порог поддержки на неё не распространяется, см. `_check_L05`
# ниже), либо кластерная ось с ДОСТАТОЧНОЙ статистической поддержкой.
#
# Это НЕ то же самое, что порог существования оси в `Grid.columns`
# (`grid._MIN_COLUMN_AXIS_SUPPORT_SHARE = 0.0015`, 0.15%) — тот отвечает на
# другой вопрос ("есть ли в шаблоне вообще такая архитектурная колонна",
# намеренно терпимый к редким, но легитимным раскладкам типа карточной
# сетки VK Tech на 63.61%, поддержанной лишь ~0.24% измерений). L05
# спрашивает более строгое: "выравнивается ли по этой оси заметная доля
# содержания шаблона, так что попадание в неё — сигнал намеренного
# выравнивания, а не шум". На насыщенном шаблоне (VK Tech — свыше 80
# кластеров-кандидатов, разведано живым замером) длинный хвост
# низкоподдержанных кластеров покрывает почти весь диапазон 0..1 при
# допуске `grid_tolerance` — порог 0.15% пропустил бы их все, и проверка
# перестала бы что-либо различать (та же болезнь, которую раньше "лечили"
# порочным отбором первых четырёх по списку).
#
# 0.01 (1%) — на порядок строже порога существования оси (0.15%), тот же
# порядок величины и то же обоснование, что и `grid._MIN_MARGIN_CLUSTER_
# SUPPORT_SHARE` (тоже 1%, тоже "обычная, не выведенная из конкретного
# числового ответа точка отсечения, на порядок больше, чем может дать
# случайное совпадение одного-двух измерений, но не требующая почти
# безусловного доминирования одной координаты"). Проверено, что порог не
# отбрасывает ровно тот случай, который он обязан пропускать (обязательный
# осмотр задачи): на VK WorkSpace реальная колонная ось с поддержкой 9
# измерений из 352 (confidence≈2.6%) проходит порог 1% с заметным запасом.
def _check_L05(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    tol = config.layout.grid_tolerance
    grid_model = profile.grid
    min_share = config.layout.grid_axis_min_support_share
    worthy_columns = [
        c for c in grid_model.columns
        if c.source == "guide" or c.confidence >= min_share
    ]

    references_left = [grid_model.margin_left] + [c.center for c in worthy_columns]
    references_right = [1.0 - grid_model.margin_right] + [c.center for c in worthy_columns]

    findings = []
    for item in ctx.items:
        if item.kind != "shape" or not item.text.strip():
            continue
        left_ok = any(abs(item.box.left - ref) <= tol for ref in references_left)
        right_ok = any(abs(item.box.right - ref) <= tol for ref in references_right)
        if left_ok or right_ok:
            continue
        findings.append(_finding(
            "L05", "minor", ctx, item,
            f"Блок «{item.name or item.shape_id}» не выровнен ни по одной направляющей "
            "восстановленной сетки шаблона (поля/колонны).",
            item.box, True, "Выровнять левый или правый край блока по ближайшему полю/колонне сетки.",
        ))
    return findings


# ---------------------------------------------------------------------------
# L06 — контент заходит в поля у краёв
# ---------------------------------------------------------------------------


def _check_L06(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    """Только ЛЕВОЕ/ПРАВОЕ поле, не верхнее/нижнее — намеренное сужение
    (ТЗ разрешает "сокращать набор проверок с обоснованием", здесь —
    сокращение области ОДНОЙ проверки, не всей проверки). `Grid.margin_top`/
    `margin_bottom` мининг усредняет по ВСЕМ ролям контента сразу
    (заголовки, тела, подписи — см. докстроку `template/grid.py`), и
    заголовок легитимно стоит заметно ВЫШЕ этого усреднённого "верхнего
    поля" (измерено на VK Tech: `margin_top`≈23.5% высоты холста против
    заголовка на 5.6% — учтён "верхним полем" смеси заголовков и тел
    целиком, а не то место, где ДОЛЖЕН начинаться именно заголовок). Левое
    и правое поле такой путаницы ролей не несут — оба края текста читаются
    по одной и той же горизонтальной линии независимо от роли блока, и
    именно левое/правое поле — то, что обычно имеют в виду словом "поля"
    вёрстки страницы."""
    tol = config.layout.margin_tolerance
    grid_model = profile.grid

    findings = []
    for item in ctx.items:
        if item.kind == "connector":
            continue
        if not (item.text.strip() or item.kind in ("picture", "graphic_frame")):
            continue  # декоративные плашки без текста намеренно доходят до краёв
        b = item.box
        if b.right > 1 + _BOUNDS_EPS or b.left < -_BOUNDS_EPS:
            continue  # это L01, не L06
        breach = b.left < grid_model.margin_left - tol or b.right > 1 - grid_model.margin_right + tol
        if not breach:
            continue
        findings.append(_finding(
            "L06", "minor", ctx, item,
            f"Содержимое «{item.name or item.shape_id}» заходит в поле у края слайда.",
            b, True, "Сдвинуть содержимое внутрь полей шаблона.",
        ))
    return findings


# ---------------------------------------------------------------------------
# L07 — картинка растянута, пропорции нарушены
# ---------------------------------------------------------------------------


def _check_L07(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    tol = config.layout.aspect_tolerance
    findings = []
    for item in ctx.items:
        if item.kind != "picture" or item.pptx_shape is None:
            continue
        try:
            native_w, native_h = item.pptx_shape.image.size
        except Exception:  # noqa: BLE001 — формат без декодируемых Pillow пикселей (emf/wmf и т.п.)
            continue
        if not native_w or not native_h:
            continue
        placed_w = item.box.width * ctx.canvas.width_in
        placed_h = item.box.height * ctx.canvas.height_in
        if placed_h <= 0 or native_h <= 0:
            continue
        native_aspect = native_w / native_h
        placed_aspect = placed_w / placed_h
        if native_aspect <= 0:
            continue
        deviation = abs(placed_aspect / native_aspect - 1.0)
        if deviation > tol:
            findings.append(_finding(
                "L07", "minor", ctx, item,
                f"Картинка «{item.name or item.shape_id}» растянута — пропорции {placed_aspect:.2f} "
                f"вместо родных {native_aspect:.2f} (отклонение {deviation:.0%}).",
                item.box, True, "Вписать картинку в рамку с сохранением пропорций (обрезать, не тянуть).",
            ))
    return findings


# ---------------------------------------------------------------------------
# T01 — гарнитура не из шаблона / больше двух гарнитур на слайде
# ---------------------------------------------------------------------------


def _check_T01(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    allowed = set(profile.type_scale.families) | set(profile.type_scale.mono)
    foreign: dict[str, tuple[_Item, set[str]]] = {}
    all_families: set[str] = set()

    for item in _text_shapes(ctx):
        families = _all_run_families(item.element)
        if not families:
            continue
        all_families |= families
        bad = families - allowed
        if bad:
            foreign[item.shape_id] = (item, bad)

    # Task 11 повторное ревью, находка №6 — та же проверка ВНУТРИ таблиц/
    # графиков (см. докстроку раздела "Текст внутри таблиц и графиков").
    for item in _aux_items(ctx):
        families = {r.family for r in _aux_style_records(item, ctx.scheme, ctx.clr_map) if r.family}
        if not families:
            continue
        all_families |= families
        bad = families - allowed
        if bad:
            foreign[item.shape_id] = (item, bad)

    findings = []
    for item, bad in foreign.values():
        findings.append(_finding(
            "T01", "major", ctx, item,
            f"Гарнитура {sorted(bad)} не входит в шрифты шаблона {sorted(allowed)}.",
            item.box, True, "Заменить гарнитуру на шрифт из шаблона.",
        ))
    if len(all_families) > config.template.max_font_families:
        findings.append(_finding(
            "T01", "major", ctx, None,
            f"На слайде использовано {len(all_families)} разных гарнитур {sorted(all_families)} — "
            f"больше {config.template.max_font_families}.",
            None, True, "Свести число гарнитур на слайде к шкале шаблона (не больше двух).",
        ))
    return findings


def _all_run_families(sp_element) -> set[str]:
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return set()
    out = set()
    for r in tx_body.iter(qn("a:r")):
        r_pr = r.find(qn("a:rPr"))
        if r_pr is None:
            continue
        latin = r_pr.find(qn("a:latin"))
        if latin is not None and latin.get("typeface"):
            out.add(latin.get("typeface"))
    return out


def _text_shapes(ctx: _SlideContext):
    return [item for item in ctx.items if item.kind == "shape" and item.text.strip()]


# ---------------------------------------------------------------------------
# Текст ВНУТРИ таблиц и графиков — T01/T02/T03/T06 читают его тоже
# ---------------------------------------------------------------------------
#
# Найдено повторным код-ревью (Task 11): `graphic_frame` (таблица/график)
# исключён из `_text_shapes` намеренно (докстрока `_make_item`/`_Item` — там
# текст собирается только для I06/D02 через `_table_text`), и до этой
# правки T01 (шрифт)/T02 (кегль)/T03 (цвет)/T06 (контраст) физически не
# видели НИ СИМВОЛА текста внутри таблиц и графиков — а это заметная часть
# типичного слайда, и ИМЕННО там нашёлся реальный денормированный кегль
# (Task 10, см. `TemplateProfile.denorm_pt`/коммит "денормировать кегль
# шкалы шаблона в графиках/таблицах/схемах") — проверки, которые обязаны
# были такое ловить, его не видели ПРИНЦИПИАЛЬНО, не по случайности.
#
# Источники текста двух РАЗНЫХ устройств:
#   - таблица: `a:tc/a:txBody/a:p/a:r` — буквальные ряды с собственным
#     `a:rPr`, СТРУКТУРНО идентичные ряду обычного текстового шейпа
#     (`p:txBody/a:p/a:r`) — та же арифметика голосования по числу
#     символов работает без изменений, не только тэг обёртки другой
#     (`a:txBody` вместо `p:txBody`).
#   - график: DrawingML-текст встречается в ДВУХ разных формах.
#     Буквальные ряды (`c:rich/a:p/a:r/a:rPr`) — заголовок графика и
#     заголовки осей (`_apply_axes`/`_color_title_runs` в `compose/
#     charts.py` пишут именно так). Область "стиль по умолчанию для
#     текста, у которого нет собственных символов в разметке"
#     (`c:txPr/a:p/a:pPr/a:defRPr`) — подписи делений осей, легенда,
#     подписи данных: и `_apply_axes` (`axis.tick_labels.font...`), и
#     дефолтный `c:chart/c:txPr`, который наследует легенда без
#     собственного оформления, устроены именно так в OOXML — там НЕТ
#     буквального `a:r/a:t`, есть только "если появится текст, он будет
#     таким". Собирать оба вида отдельным путём НА КАЖДОЕ из трёх мест
#     (подписи оси, легенда, подписи данных) избыточно и хрупко — вместо
#     этого обходится ВЕСЬ XML графика (`chart.element.iter(...)`) в
#     поиске ЛЮБОГО `c:rich` и ЛЮБОГО `c:txPr`, где бы они ни лежали:
#     признак сам по себе однозначен ("здесь есть текстовое оформление"),
#     и полный обход надёжнее точечного перечисления путей, которое на
#     незнакомом чужом файле неизбежно забыло бы какое-то из мест.
#
# Известное ограничение (честно, не молча): виден только текст, у которого
# ГДЕ-ТО в разметке есть явный атрибут (`a:rPr`/`a:defRPr` с `sz`/`a:latin`/
# `a:solidFill`) — тот же принцип, что и для обычных текстовых шейпов
# (`_dominant_run_style`/`_dominant_run_color` точно так же пропускают run
# без явного `a:rPr`, см. их докстроки). Гарнитура/кегль/цвет, унаследованные
# ЦЕЛИКОМ от темы презентации без единого явного атрибута где-либо в файле
# (крайне редкий случай — и `compose/charts.py`/`tables.py` всегда пишут
# явные атрибуты, и типичный чужой файл, отредактированный вручную в
# PowerPoint, тоже почти всегда получает явный `sz`/цвет при любом
# форматировании) — этим способом не увидеть; тот же класс ограничения,
# что уже документирован для обычного текста, не новая дыра.


@dataclass(frozen=True)
class _StyleRecord:
    """Один найденный (гарнитура, кегль, полужирность, цвет) — из
    буквального `a:rPr` ряда ИЛИ из `a:defRPr` области стиля по умолчанию
    (см. докстроку раздела выше). Любое поле может отсутствовать (`None`),
    если конкретный атрибут не задан явно в разметке — потребитель сам
    решает, какие поля ему нужны."""
    family: str | None
    size_pt: float | None
    bold: bool
    color: Color | UnresolvedColor | None


def _style_from_rpr(rpr_el, scheme: dict, clr_map: dict) -> _StyleRecord:
    """`a:rPr` (ряд) и `a:defRPr` (стиль по умолчанию области) — один и тот
    же набор атрибутов/дочерних элементов по схеме OOXML (оба — вариант
    `CT_TextCharacterProperties`), эта функция читает оба одинаково."""
    sz_raw = rpr_el.get("sz")
    size_pt = int(sz_raw) / 100 if sz_raw is not None else None
    latin = rpr_el.find(qn("a:latin"))
    family = latin.get("typeface") if latin is not None else None
    bold = rpr_el.get("b") == "1"
    fill_node = _find_fill_node(rpr_el)
    color = resolve_color(fill_node, scheme, clr_map) if fill_node is not None else None
    return _StyleRecord(family=family, size_pt=size_pt, bold=bold, color=color)


def _table_style_records(table, scheme: dict, clr_map: dict) -> list[_StyleRecord]:
    records = []
    for row in table.rows:
        for cell in row.cells:
            container = cell.text_frame._txBody  # noqa: SLF001 — см. докстроку раздела: `a:tc/a:txBody`, тот же тег, что и у обычного текстового шейпа, просто под `a:`, не `p:`
            for r in container.iter(qn("a:r")):
                r_pr = r.find(qn("a:rPr"))
                if r_pr is None:
                    continue
                records.append(_style_from_rpr(r_pr, scheme, clr_map))
    return records


def _chart_style_records(chart, scheme: dict, clr_map: dict) -> list[_StyleRecord]:
    root = chart.element
    records = []
    for rich in root.iter(qn("c:rich")):
        for r in rich.iter(qn("a:r")):
            r_pr = r.find(qn("a:rPr"))
            if r_pr is None:
                continue
            records.append(_style_from_rpr(r_pr, scheme, clr_map))
    for tx_pr in root.iter(qn("c:txPr")):
        for d in tx_pr.iter(qn("a:defRPr")):
            records.append(_style_from_rpr(d, scheme, clr_map))
    return records


def _aux_style_records(item: _Item, scheme: dict, clr_map: dict) -> list[_StyleRecord]:
    """Пустой список для обычных текстовых шейпов (они уже покрыты
    `_text_shapes`/`_dominant_run_*`/`_all_run_*`) — непусто только для
    таблиц/графиков (см. докстроку раздела)."""
    if item.is_table and item.pptx_shape is not None:
        try:
            return _table_style_records(item.pptx_shape.table, scheme, clr_map)
        except Exception:  # noqa: BLE001 — битая/недоступная таблица не должна ронять весь аудит слайда
            return []
    if item.is_chart and item.pptx_shape is not None:
        try:
            return _chart_style_records(item.pptx_shape.chart, scheme, clr_map)
        except Exception:  # noqa: BLE001 — битый/недоступный график не должен ронять весь аудит слайда
            return []
    return []


def _dominant_aux_style(records: list[_StyleRecord]) -> tuple[str, float, bool] | None:
    """Аналог `_dominant_run_style`, но голосование по ЧИСЛУ ЗАПИСЕЙ, не по
    числу символов — у `a:defRPr`-записей (см. `_StyleRecord`) нет
    привязанного текста, чтобы посчитать его длину, только сам факт
    "область такого-то размера оформлена так-то"."""
    votes: dict[tuple[str, float, bool], int] = {}
    for r in records:
        if r.family and r.size_pt:
            key = (r.family, r.size_pt, r.bold)
            votes[key] = votes.get(key, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def _dominant_aux_color(records: list[_StyleRecord]) -> Color | UnresolvedColor | None:
    for r in records:
        if isinstance(r.color, Color):
            return r.color
    return None


def _aux_items(ctx: _SlideContext):
    return [item for item in ctx.items if item.is_table or item.is_chart]


# ---------------------------------------------------------------------------
# T02 — кегль не из типографической шкалы шаблона
# ---------------------------------------------------------------------------


def _allowed_font_sizes(profile: TemplateProfile) -> set[float]:
    """Кегли, легитимные для этого шаблона — денормированные ступени
    `TypeScale.steps` ПЛЮС денормированные родные размеры всех намайненных
    слотов паттернов.

    Почему не только шесть именованных ступеней: `compose/builder.py`
    (`_shrink_sequence`) кладёт текст РОДНЫМ кеглем намайненного слота
    ПЕРВЫМ кандидатом — на ступень шкалы текст ужимается, только если
    родной кегль не влезает. Родной кегль слота — реальный, измеренный
    размер шрифта КОНКРЕТНОГО шейпа шаблона (`template/patterns.py::
    _shape_dominant_size`), а не один из шести круглых чисел шкалы — на
    практике он почти никогда не совпадает с ними день в день. Проверка
    "кегль обязан быть ОДНИМ ИЗ ШЕСТИ ИМЕНОВАННЫХ ступеней" ловила бы
    легитимный вывод собственной сборки как брак. Честное прочтение "не из
    шкалы шаблона" — "нет нигде в реальном типографическом инвентаре этого
    шаблона" (ни в ступенях, ни в фактических кеглях его собственных
    слайдов), не "не совпадает с одним из шести круглых чисел"."""
    sizes = {profile.denorm_pt(v) for v in profile.type_scale.steps.values() if v > 0}
    for pattern in profile.patterns:
        for slot in pattern.slots:
            if slot.size_pt and slot.size_pt > 0:
                sizes.add(profile.denorm_pt(slot.size_pt))
    return sizes


def _check_T02(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    allowed = _allowed_font_sizes(profile)
    if not allowed:
        return []
    tol = config.template.size_tolerance_pt
    findings = []
    for item in _text_shapes(ctx):
        sizes = _all_run_sizes(item.element)
        for size_pt in sizes:
            if any(abs(size_pt - a) <= tol for a in allowed):
                continue
            findings.append(_finding(
                "T02", "major", ctx, item,
                f"Кегль {size_pt:g}pt не входит в типографическую шкалу шаблона.",
                item.box, True, "Заменить кегль на ближайшую ступень шкалы шаблона.",
            ))
            break  # один finding на шейп достаточно, даже если офф-шкальных кеглей несколько

    # Task 11 повторное ревью, находка №6 — та же проверка ВНУТРИ таблиц/
    # графиков (см. докстроку раздела "Текст внутри таблиц и графиков").
    # Именно здесь на демонстрационной колоде находился реальный
    # денормированный кегль (Task 10 фикс) — T02 раньше физически не мог
    # его увидеть.
    for item in _aux_items(ctx):
        sizes = {r.size_pt for r in _aux_style_records(item, ctx.scheme, ctx.clr_map) if r.size_pt}
        for size_pt in sizes:
            if any(abs(size_pt - a) <= tol for a in allowed):
                continue
            findings.append(_finding(
                "T02", "major", ctx, item,
                f"Кегль {size_pt:g}pt внутри таблицы/графика не входит в типографическую шкалу шаблона.",
                item.box, True, "Заменить кегль на ближайшую ступень шкалы шаблона.",
            ))
            break
    return findings


def _all_run_sizes(sp_element) -> set[float]:
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return set()
    out = set()
    for r in tx_body.iter(qn("a:r")):
        r_pr = r.find(qn("a:rPr"))
        sz_raw = r_pr.get("sz") if r_pr is not None else None
        if sz_raw is not None:
            out.add(int(sz_raw) / 100)
    return out


# ---------------------------------------------------------------------------
# T03 — цвет не из палитры шаблона
# ---------------------------------------------------------------------------


def _check_T03(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    text_allowed = {c.upper() for c in profile.palette_roles.values() if c}
    fill_allowed = text_allowed | {c.upper() for c in profile.theme.scheme.values() if c} | {
        c.upper() for c in profile.chart_series if c
    }
    findings = []
    for item in ctx.items:
        if item.kind == "shape":
            tx_body = item.element.find(qn("p:txBody"))
            if tx_body is not None:
                for r in tx_body.iter(qn("a:r")):
                    r_pr = r.find(qn("a:rPr"))
                    if r_pr is None:
                        continue
                    fill = r_pr.find(qn("a:solidFill"))
                    if fill is None:
                        continue
                    color = resolve_color(fill, ctx.scheme, ctx.clr_map)
                    if isinstance(color, Color) and color.hex.upper() not in text_allowed:
                        findings.append(_finding(
                            "T03", "major", ctx, item,
                            f"Цвет текста {color.hex} не входит в палитру шаблона.",
                            item.box, True, "Заменить цвет на ближайший из палитры шаблона.",
                        ))
                        break
            if isinstance(item.fill_color, Color) and item.fill_color.hex.upper() not in fill_allowed:
                findings.append(_finding(
                    "T03", "major", ctx, item,
                    f"Заливка «{item.name or item.shape_id}» цветом {item.fill_color.hex} "
                    "не входит в палитру шаблона.",
                    item.box, True, "Заменить заливку на цвет из палитры шаблона.",
                ))
        elif item.is_table or item.is_chart:
            # Task 11 повторное ревью, находка №6 — та же проверка цвета
            # ТЕКСТА внутри таблиц/графиков (см. докстроку раздела "Текст
            # внутри таблиц и графиков"). Заливка ячеек/точек графика — вне
            # объёма этой правки (не была запрошена и не является текстом).
            for rec in _aux_style_records(item, ctx.scheme, ctx.clr_map):
                if isinstance(rec.color, Color) and rec.color.hex.upper() not in text_allowed:
                    findings.append(_finding(
                        "T03", "major", ctx, item,
                        f"Цвет текста {rec.color.hex} внутри таблицы/графика не входит в палитру шаблона.",
                        item.box, True, "Заменить цвет на ближайший из палитры шаблона.",
                    ))
                    break
    return findings


# ---------------------------------------------------------------------------
# T04 — слайд собран не на макете из шаблона
# ---------------------------------------------------------------------------


def _check_T04(ctx: _SlideContext, profile: TemplateProfile) -> list[Finding]:
    if ctx.layout_entry is not None:
        return []
    return [_finding(
        "T04", "major", ctx, None,
        f"Слайд собран на макете {ctx.layout_part_name!r}, которого нет в шаблоне.",
        None, False, "Пересобрать слайд на одном из макетов шаблона.",
    )]


# ---------------------------------------------------------------------------
# T05 — логотип или колонтитул сдвинуты с положенного места
# ---------------------------------------------------------------------------


def _check_T05(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    findings = []
    logo = profile.assets.logo
    if logo is not None and profile.assets.logo_placements:
        tol = config.template.logo_position_tolerance
        logo_bytes = _logo_bytes(profile)
        for item in ctx.items:
            if item.kind != "picture" or item.pptx_shape is None or logo_bytes is None:
                continue
            try:
                blob = item.pptx_shape.image.blob
            except Exception:  # noqa: BLE001
                continue
            if blob != logo_bytes:
                continue
            on_place = any(
                abs(item.box.left - p.box.left) <= tol and abs(item.box.top - p.box.top) <= tol
                for p in profile.assets.logo_placements
            )
            if not on_place:
                findings.append(_finding(
                    "T05", "major", ctx, item,
                    "Логотип сдвинут с положенного по шаблону места.",
                    item.box, True, "Вернуть логотип на положенное шаблоном место.",
                ))

    ftr_boxes = {p.ph_type: p.box for p in (ctx.layout_entry.placeholders if ctx.layout_entry else []) if p.ph_type in ("ftr", "sldNum", "dt")}
    for item in ctx.items:
        if item.kind != "shape":
            continue
        ph = _placeholder_element(item.element)
        if ph is None:
            continue
        ph_type = ph.get("type")
        if ph_type not in ftr_boxes:
            continue
        expected = ftr_boxes[ph_type]
        tol = config.template.logo_position_tolerance
        if abs(item.box.left - expected.left) > tol or abs(item.box.top - expected.top) > tol:
            findings.append(_finding(
                "T05", "major", ctx, item,
                f"Колонтитул «{ph_type}» сдвинут с положенного по макету места.",
                item.box, True, "Вернуть колонтитул на положенное макетом место.",
            ))
    return findings


def _placeholder_element(sp_element):
    nv_sp_pr = sp_element.find(qn("p:nvSpPr"))
    nv_pr = nv_sp_pr.find(qn("p:nvPr")) if nv_sp_pr is not None else None
    return nv_pr.find(qn("p:ph")) if nv_pr is not None else None


def _logo_bytes(profile: TemplateProfile) -> bytes | None:
    if not profile.assets.logo or not profile.source_path:
        return None
    try:
        with PptxPackage.open(Path(profile.source_path)) as pkg:
            return pkg.part(profile.assets.logo.part_name)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# T06 — контраст текста к фону ниже 4.5:1 (3:1 для крупного текста)
# ---------------------------------------------------------------------------


def _check_T06(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    cfg = config.template
    findings = []
    for item in _text_shapes(ctx):
        style = _dominant_run_style(item.element)
        text_color = _dominant_run_color(item.element, ctx.scheme, ctx.clr_map)
        finding = _contrast_finding(ctx, item, style, text_color, config, "")
        if finding is not None:
            findings.append(finding)

    # Task 11 повторное ревью, находка №6 — та же проверка ВНУТРИ таблиц/
    # графиков (см. докстроку раздела "Текст внутри таблиц и графиков").
    # "Доминантный" стиль/цвет здесь — ПЕРВАЯ найденная запись с цветом
    # (`_dominant_aux_color`), не голосование по площади текста, как для
    # обычных шейпов: у `a:defRPr`-записей нет текста, чтобы взвесить его
    # длиной (см. докстроку `_dominant_aux_style`) — честное упрощение,
    # тот же принцип, что уже документирован у `_background_under`
    # ("один фон на блок").
    for item in _aux_items(ctx):
        records = _aux_style_records(item, ctx.scheme, ctx.clr_map)
        style = _dominant_aux_style(records)
        text_color = _dominant_aux_color(records)
        finding = _contrast_finding(ctx, item, style, text_color, config, " внутри таблицы/графика")
        if finding is not None:
            findings.append(finding)
    return findings


def _contrast_finding(
    ctx: _SlideContext, item: _Item, style: tuple[str, float, bool] | None,
    text_color: Color | UnresolvedColor | None, config: AuditConfig, label_suffix: str,
) -> Finding | None:
    cfg = config.template
    if style is None or not isinstance(text_color, Color):
        return None
    _family, size_pt, bold = style
    bg_hex, bg_luminance = _background_under(item, ctx, config)
    if bg_hex is not None:
        ratio = contrast_ratio(bg_hex, text_color.hex)
    else:
        ratio = _contrast_from_luminance(_relative_luminance(text_color.hex), bg_luminance)
    is_large = size_pt >= cfg.large_text_pt or (bold and size_pt >= cfg.large_bold_pt)
    threshold = cfg.min_contrast_large if is_large else cfg.min_contrast_small
    if ratio >= threshold:
        return None
    return _finding(
        "T06", "critical", ctx, item,
        f"Контраст текста «{item.name or item.shape_id}»{label_suffix} к фону {ratio:.1f}:1 "
        f"ниже требуемых {threshold:.1f}:1.",
        item.box, True, "Заменить цвет текста на контрастный цвет из палитры шаблона.",
    )


def _dominant_run_color(sp_element, scheme: dict, clr_map: dict) -> Color | UnresolvedColor | None:
    tx_body = sp_element.find(qn("p:txBody"))
    if tx_body is None:
        return None
    for r in tx_body.iter(qn("a:r")):
        r_pr = r.find(qn("a:rPr"))
        if r_pr is None:
            continue
        fill = r_pr.find(qn("a:solidFill"))
        if fill is None:
            continue
        color = resolve_color(fill, scheme, clr_map)
        if isinstance(color, Color):
            return color
    return None


def _background_under(item: _Item, ctx: _SlideContext, config: AuditConfig) -> tuple[str | None, float]:
    """Фон НЕПОСРЕДСТВЕННО под текстовым блоком: своя заливка → объемлющая
    плашка (самая маленькая) → фон слайда → фон макета. `colorpick.
    slide_background_luminance` уже даёт последнее звено (фон макета,
    Task 10) — эта функция переиспользует его как запасной вариант и
    подставляет более локальный источник, когда он находится."""
    if isinstance(item.fill_color, Color):
        return item.fill_color.hex, _relative_luminance(item.fill_color.hex)

    candidates = [
        other for other in ctx.items
        if other is not item and other.kind == "shape" and isinstance(other.fill_color, Color)
        and _fully_contains(other.box, item.box, config.layout.margin_tolerance)
    ]
    if candidates:
        plate = min(candidates, key=lambda o: o.box.width * o.box.height)
        return plate.fill_color.hex, _relative_luminance(plate.fill_color.hex)

    if ctx.bg_hex is not None:
        return ctx.bg_hex, _relative_luminance(ctx.bg_hex)
    return None, ctx.bg_luminance


# ---------------------------------------------------------------------------
# D01/D02 — плотность буллетов
# ---------------------------------------------------------------------------


def _bulleted_paragraphs(item: _Item):
    if item.kind != "shape":
        return
    tx_body = item.element.find(qn("p:txBody"))
    if tx_body is None:
        return
    for p_el in tx_body.findall(qn("a:p")):
        if _paragraph_is_bulleted(p_el):
            yield p_el


def _paragraph_is_bulleted(p_el) -> bool:
    p_pr = p_el.find(qn("a:pPr"))
    if p_pr is None:
        return False
    if p_pr.find(qn("a:buNone")) is not None:
        return False
    return p_pr.find(qn("a:buChar")) is not None or p_pr.find(qn("a:buAutoNum")) is not None


def _check_D01(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    total = sum(1 for item in ctx.items for _ in _bulleted_paragraphs(item))
    if total <= config.density.max_bullets:
        return []
    return [_finding(
        "D01", "minor", ctx, None,
        f"На слайде {total} буллетов — больше {config.density.max_bullets}.",
        None, False, "Сократить число буллетов или разбить содержание на два слайда.",
    )]


def _check_D02(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    findings = []
    for item in ctx.items:
        for p_el in _bulleted_paragraphs(item):
            text = _paragraph_text(p_el)
            words = [w for w in text.split() if w]
            if len(words) > config.density.max_words_per_bullet:
                shown = f"{text[:60]}…" if len(text) > 60 else text
                findings.append(_finding(
                    "D02", "minor", ctx, item,
                    f"Буллет из {len(words)} слов длиннее {config.density.max_words_per_bullet}: «{shown}».",
                    item.box, False, "Сократить формулировку буллета.",
                ))
    return findings


# ---------------------------------------------------------------------------
# D03 — таблица больше 7 строк или 5 колонок
# ---------------------------------------------------------------------------


def _check_D03(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    findings = []
    for item in ctx.items:
        if not item.is_table or item.pptx_shape is None:
            continue
        table = item.pptx_shape.table
        n_rows, n_cols = len(table.rows), len(table.columns)
        if n_rows > config.density.max_table_rows or n_cols > config.density.max_table_cols:
            findings.append(_finding(
                "D03", "minor", ctx, item,
                f"Таблица {n_rows}×{n_cols} больше нормы {config.density.max_table_rows}×"
                f"{config.density.max_table_cols}.",
                item.box, False, "Сократить таблицу или разбить на несколько.",
            ))
    return findings


# ---------------------------------------------------------------------------
# D04 — больше 5 серий на диаграмме
# ---------------------------------------------------------------------------


def _check_D04(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    findings = []
    for item in ctx.items:
        if not item.is_chart or item.pptx_shape is None:
            continue
        try:
            chart = item.pptx_shape.chart
            n_series = len(chart.plots[0].series) if chart.plots else 0
        except Exception:  # noqa: BLE001
            continue
        if n_series > config.density.max_chart_series:
            findings.append(_finding(
                "D04", "minor", ctx, item,
                f"На диаграмме {n_series} серий — больше {config.density.max_chart_series}.",
                item.box, False, "Сократить число серий или сменить тип графика.",
            ))
    return findings


# ---------------------------------------------------------------------------
# D05 — слайд заполнен меньше четверти или больше трёх четвертей
# ---------------------------------------------------------------------------


def slide_fill_ratio(slide, canvas: Canvas, profile: TemplateProfile, *, index: int = 0) -> float:
    """Доля холста, занятая видимым содержанием уже уложенного слайда —
    ровно то число, по которому D05 судит «пусто/перегружено».

    Вынесено из `_check_D05` отдельной функцией, потому что то же число
    нужно инструменту `compose.slide_tools.try_slide`: агент спрашивает
    «насколько полным вышел слайд» и обязан получить тот же ответ, который
    потом даст аудит. Два расчёта заполненности разошлись бы — инструмент
    говорил бы 7%, аудит 14%, и агент правил бы не то (ровно это и вышло
    на первом живом прогоне инструмента 23 сентября 2026)."""
    ctx = _build_context_inmemory(index, slide, canvas, profile)
    return _fill_ratio(ctx)


def _fill_ratio(ctx: _SlideContext) -> float:
    canvas_area = ctx.canvas.width_in * ctx.canvas.height_in
    if canvas_area <= 0:
        return 0.0
    covered = 0.0
    for item in ctx.items:
        if item.kind == "connector":
            continue
        if item.kind == "shape" and (item.text.strip() or item.has_fill):
            # `_effective_box` сама решает, усаживать ли по измеренному
            # тексту (голый текст без заливки) или отдать рамку как есть
            # (своя заливка — видимые чернила на весь бокс, см. её докстроку).
            eff = _effective_box(item, ctx.canvas)
            covered += eff.area * canvas_area
        elif item.kind in ("picture", "graphic_frame"):
            covered += item.box.area * canvas_area
    return covered / canvas_area


def _check_D05(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    if ctx.canvas.width_in * ctx.canvas.height_in <= 0:
        return []
    ratio = _fill_ratio(ctx)
    cfg = config.density
    if cfg.fill_ratio_min <= ratio <= cfg.fill_ratio_max:
        return []
    direction = "меньше четверти" if ratio < cfg.fill_ratio_min else "больше трёх четвертей"
    return [_finding(
        "D05", "minor", ctx, None,
        f"Слайд заполнен на {ratio:.0%} холста — {direction}.",
        Box(0.0, 0.0, 1.0, 1.0), False, "Подобрать другую раскладку или изменить объём содержания.",
    )]


# ---------------------------------------------------------------------------
# I02 — остался текст-заглушка
# ---------------------------------------------------------------------------


def _check_I02(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    patterns = [p.lower() for p in config.integrity.placeholder_patterns]
    findings = []
    for item in ctx.items:
        text = item.text.lower()
        if not text.strip():
            continue
        hit = next((p for p in patterns if p in text), None)
        if hit:
            findings.append(_finding(
                "I02", "major", ctx, item,
                f"Остался текст-заглушка «{hit}» в «{item.name or item.shape_id}».",
                item.box, False, "Заменить заглушку на реальный контент.",
            ))
    return findings


# ---------------------------------------------------------------------------
# I03 — пустой слайд или слайд с одним заголовком
# ---------------------------------------------------------------------------


def _check_I03(ctx: _SlideContext, profile: TemplateProfile, config: AuditConfig) -> list[Finding]:
    meaningful = [
        item for item in ctx.items
        if (item.kind == "shape" and item.text.strip()) or item.kind == "picture" or item.kind == "graphic_frame"
    ]
    if not meaningful:
        return [_finding(
            "I03", "major", ctx, None, "Слайд пуст — на нём нет ни текста, ни изображения, ни таблицы/графика.",
            Box(0.0, 0.0, 1.0, 1.0), False, "Добавить содержание слайду.",
        )]
    if len(meaningful) > 1:
        return []
    only = meaningful[0]
    if only.kind != "shape":
        return []
    style = _dominant_run_style(only.element)
    if style is None:
        return []
    heading_pt = profile.type_scale_pt("h2", 0.0)
    tol = config.template.size_tolerance_pt
    _family, size_pt, _bold = style
    if heading_pt > 0 and size_pt >= heading_pt - tol:
        return []  # крупный одиночный текст — легитимный титульный/разделительный слайд
    return [_finding(
        "I03", "major", ctx, only, "На слайде — только один небольшой текстовый блок, содержания нет.",
        only.box, False, "Добавить содержание слайду.",
    )]


# ---------------------------------------------------------------------------
# I04 — слайд оказался картинкой, а не редактируемыми объектами
# ---------------------------------------------------------------------------


def _check_I04(ctx: _SlideContext, config: AuditConfig) -> list[Finding]:
    pictures = [item for item in ctx.items if item.kind == "picture"]
    others = [
        item for item in ctx.items
        if item.kind != "picture" and item.kind != "connector"
        and ((item.kind == "shape" and item.text.strip()) or item.kind == "graphic_frame")
    ]
    if others or not pictures:
        return []
    big = [p for p in pictures if p.box.area >= config.integrity.single_picture_coverage]
    if not big:
        return []
    item = big[0]
    return [_finding(
        "I04", "critical", ctx, item,
        "Слайд выгружен единой картинкой — редактируемых объектов на нём нет.",
        item.box, False, "Заменить растр на редактируемые объекты (текст/график/таблицу).",
    )]


# ---------------------------------------------------------------------------
# I05 — у диаграммы нет подписей осей, единиц или легенды
# ---------------------------------------------------------------------------


def _check_I05(ctx: _SlideContext) -> list[Finding]:
    findings = []
    for item in ctx.items:
        if not item.is_chart or item.pptx_shape is None:
            continue
        try:
            chart = item.pptx_shape.chart
        except Exception:  # noqa: BLE001
            continue
        missing = []
        try:
            chart_type = chart.chart_type
        except Exception:  # noqa: BLE001
            continue
        is_pie_like = chart_type in _PIE_LIKE
        try:
            n_series = len(chart.plots[0].series) if chart.plots else 0
            n_categories = len(chart.plots[0].categories) if chart.plots else 0
        except Exception:  # noqa: BLE001
            n_series = n_categories = 0

        if is_pie_like:
            if n_categories > 1 and not chart.has_legend:
                missing.append("легенда")
        else:
            if n_series > 1 and not chart.has_legend:
                missing.append("легенда")
            try:
                if not chart.category_axis.has_title:
                    missing.append("подпись оси категорий")
            except Exception:  # noqa: BLE001
                pass
            try:
                if not chart.value_axis.has_title:
                    missing.append("подпись оси значений/единиц")
            except Exception:  # noqa: BLE001
                pass

        if missing:
            findings.append(_finding(
                "I05", "major", ctx, item,
                f"У диаграммы «{item.name or item.shape_id}» нет: {', '.join(missing)}.",
                item.box, True, "Добавить недостающие подписи осей/единиц/легенду.",
            ))
    return findings


# ---------------------------------------------------------------------------
# I06 — два слайда дублируют друг друга
# ---------------------------------------------------------------------------


def _check_I06(slide_texts: list[str], config: AuditConfig) -> list[Finding]:
    threshold = config.integrity.duplicate_similarity
    min_len = config.integrity.min_duplicate_check_text_len
    findings = []
    for i in range(len(slide_texts)):
        text_a = slide_texts[i].strip()
        if len(text_a) < min_len:
            continue
        for j in range(i + 1, len(slide_texts)):
            text_b = slide_texts[j].strip()
            if len(text_b) < min_len:
                continue
            ratio = SequenceMatcher(None, text_a, text_b).ratio()
            if ratio >= threshold:
                findings.append(Finding(
                    check_id="I06", severity="major", slide_index=j, shape_ref=None,
                    message=f"Слайд {j} дублирует слайд {i} (схожесть текста {ratio:.0%}).",
                    box=Box(0.0, 0.0, 1.0, 1.0), fixable=True,
                    fix_hint="Удалить один из дублирующихся слайдов.",
                ))
    return findings


# ---------------------------------------------------------------------------
# Финальная сортировка — детерминированный порядок, см. докстроку модуля.
# ---------------------------------------------------------------------------


def _stable_sort(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (
            f.slide_index if f.slide_index is not None else -1,
            f.check_id, f.shape_ref or "", f.message,
        ),
    )
