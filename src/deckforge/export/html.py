"""Экспорт готовой колоды в самодостаточный `.html`.

`to_html` не рисует слайды заново по `DeckSpec` — оно читает УЖЕ СОБРАННЫЙ
`.pptx` (`pptx_path`) напрямую по XML (`deckforge.ooxml`, тот же слой, что
разбирает шаблоны), тем же способом, каким `compose.builder` его построил:
каждый шейп → `<div>`, спозиционированный в тех же ДОЛЯХ ХОЛСТА, что и в
файле (`ooxml.walk.walk_shapes` уже прогоняет координаты через цепочку
трансформаций групп — не нужно повторять эту математику здесь). Текст —
текст (`a:t` → текстовый узел DOM, выделяется и находится поиском браузера),
таблица — `<table>`, график — инлайновый SVG с текстовыми подписями и
числами, картинка — `<img>` с данными, вшитыми `base64` из `ppt/media/`.
Ни один слайд не растеризуется целиком — то же требование ТЗ, что и к
`.pptx` ("слайд, выгруженный единым растровым изображением, не
засчитывается").

`deck_spec` при этом не описывает вёрстку (`plan/spec.py`: "здесь нет ни
одного поля, несущего координату, цвет, шрифт или кегль") — сюда он передан
только затем, чтобы подписать каждый собранный слайд его `kind`
(`data-kind`, для CSS-темизации навигации/обзора) и оставить читаемый
заголовок документа/языка. Соответствие "слайд в файле" → "SlideSpec плана"
устанавливается по вхождению `headline` в текст слайда, по порядку — не по
индексу: `compose.builder.build_deck` может пропустить слайд, для которого
не нашлось подходящего паттерна (см. докстроку `SlideSpec.findings`), и
тогда чисел `pptx`-слайдов и `deck_spec.slides` расходятся.

Дизайн-токены шаблона (`profile.palette_roles`, `profile.type_scale`)
уходят в CSS-переменные на `:root` — тот же контракт, что и в `.pptx`:
вёрстка ничего не придумывает поверх того, что нашлось в шаблоне. Гарнитура
шаблона вшивается `@font-face`/`base64`, если её TTF/OTF реально
установлен в систему (см. `_embed_fonts`); если нет — честный CSS-фолбэк на
системный sans-serif и предупреждение в `warnings` результата (см.
`HtmlExportResult`).
"""
from __future__ import annotations
import base64
import subprocess
import shutil
from dataclasses import dataclass, field
from html import escape as _esc
from math import cos, pi, sin
from pathlib import Path

from lxml import etree

from deckforge.ooxml.color import Color, resolve_color
from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes
from deckforge.plan.spec import DeckSpec, SlideSpec
from deckforge.template.profile import LayoutEntryModel, TemplateProfile

PX_PER_PT = 96 / 72  # 96 CSS-px/дюйм ÷ 72pt/дюйм — стандартный пересчёт браузера.

# Цвета-заглушки, когда роль палитры отсутствует в шаблоне (не должно
# происходить на валидном профиле — `palette_roles` всегда несёт хотя бы
# запасной вариант, см. `naming.py`), — честные нейтральные значения, а не
# падение экспорта.
_FALLBACK_INK = "#101114"
_FALLBACK_SURFACE = "#FFFFFF"
_FALLBACK_BRAND = "#0077FF"

_ALIGN_CSS = {"l": "left", "ctr": "center", "r": "right", "just": "justify"}
_ANCHOR_JUSTIFY = {"t": "flex-start", "ctr": "center", "b": "flex-end"}


@dataclass
class HtmlExportResult:
    """Возврат `to_html` вместе с честным отчётом о деградациях (брифом:
    "если шрифта нет — говори об этом в отчёте"), не только путь к файлу."""

    path: Path
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Ctx:
    pkg: PptxPackage
    slide_part: str
    canvas: Canvas
    scheme: dict[str, str]
    clr_map: dict[str, str]
    profile: TemplateProfile


def to_html(
    deck_spec: DeckSpec, profile: TemplateProfile, pptx_path: Path, out: Path,
    *, visual: "VisualAuditResult | AuditReport | None" = None,
    budget: dict | None = None, risky_slides: dict[int, dict] | None = None,
    fidelity: "FidelityReport | None" = None,
) -> Path:
    """Собирает `out` — единый `.html` без внешних запросов. Возвращает `out`
    (интерфейс брифа); подробности деградаций — `to_html_report` ниже, для
    вызывающего кода, которому нужен не только путь, но и что подставлено
    запасным вариантом.

    `visual` — необязательные оценки PPTEval задачи G (`audit.visual.
    VisualAuditResult` или уже сведённый `audit.report.AuditReport`);
    `None` (по умолчанию) держит старое поведение буквально — экспорт не
    обязан знать про аудит, чтобы работать (интерфейс брифа Task 14 старше
    самих оценок, см. докстроку `_extract_visual_scores`).

    `budget`/`risky_slides` — задача L: снимок `workflow.budget.RunBudget.
    summary()` и `workflow.visual_stage.VisualStageOutcome.summary()["risk"]`
    (позиция слайда -> {technical, semantic, total}), тоже необязательные —
    отчёт без бюджета (например, вызов из старого кода/теста) остаётся
    рабочим, просто без блока режима.

    `fidelity` — задача T: `audit.fidelity.FidelityReport` (метрики верности
    шаблону), тоже необязательный — см. `_fidelity_html`."""
    return to_html_report(
        deck_spec, profile, pptx_path, out, visual=visual, budget=budget, risky_slides=risky_slides,
        fidelity=fidelity,
    ).path


def to_html_report(
    deck_spec: DeckSpec, profile: TemplateProfile, pptx_path: Path, out: Path,
    *, visual: "VisualAuditResult | AuditReport | None" = None,
    budget: dict | None = None, risky_slides: dict[int, dict] | None = None,
    fidelity: "FidelityReport | None" = None,
) -> HtmlExportResult:
    pptx_path = Path(pptx_path)
    out = Path(out)
    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)
    layouts_by_part = {entry.part_name.lstrip("/"): entry for entry in profile.layouts}
    scheme = dict(profile.theme.scheme)
    clr_map = dict(profile.theme.clr_map)
    slide_scores, deck_score, content_avg, design_avg = _extract_visual_scores(visual)

    warnings: list[str] = []
    slides_html: list[str] = []
    with PptxPackage.open(pptx_path) as pkg:
        slide_parts = _ordered_slide_parts(pkg)
        spec_cursor = 0
        for index, part in enumerate(slide_parts):
            slide_root = pkg.xml(part)
            ctx = _Ctx(pkg=pkg, slide_part=part, canvas=canvas, scheme=scheme, clr_map=clr_map, profile=profile)
            blocks_html, full_text = _render_shapes(ctx, slide_root)

            layout_part = _related_layout_part(pkg, part)
            layout_entry = layouts_by_part.get(layout_part.lstrip("/")) if layout_part else None
            bg_css, is_dark = _slide_background(layout_entry, profile)

            spec_slide, matched_index, spec_cursor = _match_spec_slide(deck_spec, full_text, spec_cursor)
            score = slide_scores.get(matched_index) if matched_index is not None else None
            slides_html.append(
                _slide_section(index, blocks_html, bg_css, is_dark, spec_slide, score)
            )

    fonts_css, font_warnings = _embed_fonts(profile)
    warnings.extend(font_warnings)

    document = _wrap_document(
        deck_spec, profile, canvas, slides_html, fonts_css,
        deck_score=deck_score, content_avg=content_avg, design_avg=design_avg,
        budget=budget, risky_slides=risky_slides, fidelity=fidelity,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")
    return HtmlExportResult(path=out, warnings=warnings)


def _extract_visual_scores(
    visual: "VisualAuditResult | AuditReport | None",
) -> tuple[dict[int, dict], dict | None, float | None, float | None]:
    """Задача G: и `VisualAuditResult` (прямой результат `audit.visual.
    run_visual`), и уже сведённый `audit.report.AuditReport` несут одни и те
    же четыре поля с оценками PPTEval под одинаковыми именами — эта функция
    просто читает их дюк-тайпингом (`getattr` с фолбэком), не импортируя ни
    один из модулей на верхнем уровне: `export/` не обязан тянуть `audit/`
    как обязательную зависимость только ради типов в аннотации (они и так
    строковые, см. `from __future__ import annotations` модуля). `None` —
    честный случай "оценок нет вовсе" (аудит не прогонялся или его результат
    не передали), а не сигнал ошибки."""
    if visual is None:
        return {}, None, None, None
    return (
        dict(getattr(visual, "slide_scores", {}) or {}),
        getattr(visual, "deck_score", None),
        getattr(visual, "content_avg", None),
        getattr(visual, "design_avg", None),
    )


# ---------------------------------------------------------------------------
# Обход пакета: слайды по порядку, лейаут каждого слайда
# ---------------------------------------------------------------------------


def _ordered_slide_parts(pkg: PptxPackage) -> list[str]:
    pres_part = pkg.presentation_part()
    root = pkg.xml(pres_part)
    sld_id_lst = root.find(qn("p:sldIdLst"))
    if sld_id_lst is None:
        return []
    rels = pkg.rels(pres_part)
    parts = []
    for sld_id in sld_id_lst:
        rid = sld_id.get(qn("r:id"))
        part = rels.get(rid)
        if part:
            parts.append(part)
    return parts


def _related_layout_part(pkg: PptxPackage, slide_part: str) -> str | None:
    related = pkg.related(slide_part, "slideLayout")
    return related[0] if related else None


def _match_spec_slide(deck_spec: DeckSpec, full_text: str, cursor: int) -> tuple[SlideSpec | None, int | None, int]:
    """Первый ещё не сопоставленный `SlideSpec`, чей `headline` входит в
    текст этого pptx-слайда — по порядку, не по индексу (см. докстроку
    модуля: `build_deck` может пропустить слайд плана). Средний элемент
    возврата — индекс найденного `SlideSpec` в `deck_spec.slides` (задача G:
    тот же индекс, что ключ `VisualAuditResult.slide_scores` — `run_visual`
    нумерует слайды по позиции в `spec.slides`, см. `audit/visual.py`), не
    путать с новым значением курсора (третий элемент)."""
    for i in range(cursor, len(deck_spec.slides)):
        candidate = deck_spec.slides[i]
        if candidate.headline and candidate.headline in full_text:
            return candidate, i, i + 1
    return None, None, cursor


# ---------------------------------------------------------------------------
# Фон слайда — из лейаута (builder никогда не переопределяет p:bg слайда)
# ---------------------------------------------------------------------------


def _slide_background(layout_entry: LayoutEntryModel | None, profile: TemplateProfile) -> tuple[str, bool]:
    is_dark = layout_entry.is_dark if layout_entry is not None else False
    color = layout_entry.background.color if layout_entry is not None else None
    if color is not None and color.resolved and color.hex:
        return color.hex, is_dark
    fallback = _FALLBACK_INK if is_dark else profile.palette_roles.get("surface", _FALLBACK_SURFACE)
    return fallback, is_dark


# ---------------------------------------------------------------------------
# Обход шейпов слайда
# ---------------------------------------------------------------------------


def _box_style(box: Box) -> str:
    return (
        f"left:{box.left * 100:.4f}%;top:{box.top * 100:.4f}%;"
        f"width:{box.width * 100:.4f}%;height:{box.height * 100:.4f}%;"
    )


def _graphic_data(el):
    graphic = el.find(qn("a:graphic"))
    if graphic is None:
        return None
    return graphic.find(qn("a:graphicData"))


def _txbody_has_text(txbody) -> bool:
    return any((r.findtext(qn("a:t")) or "").strip() for r in txbody.iter(qn("a:r")))


def _render_shapes(ctx: _Ctx, slide_root) -> tuple[str, str]:
    blocks: list[str] = []
    text_chunks: list[str] = []
    for ref in walk_shapes(slide_root, ctx.canvas, include_groups=False):
        if ref.box is None:
            continue  # координаты не резолвятся (сломанная группа/нет xfrm) — честно пропускаем
        el = ref.element
        style = _box_style(ref.box)

        if ref.kind == "shape":
            txbody = el.find(qn("p:txBody"))
            if txbody is not None and _txbody_has_text(txbody):
                text_html, plain = _render_text_shape(ctx, txbody)
                if plain.strip():
                    text_chunks.append(plain)
                fill = _shape_fill_css(ctx, el)
                blocks.append(f'<div class="block block--text" style="{style}{fill}">{text_html}</div>')
            else:
                fill = _shape_fill_css(ctx, el)
                if fill:
                    blocks.append(f'<div class="block block--decor" style="{style}{fill}"></div>')
        elif ref.kind == "connector":
            line = _connector_css(ctx, el)
            if line:
                blocks.append(f'<div class="block block--connector" style="{style}{line}"></div>')
        elif ref.kind == "picture":
            img = _render_picture(ctx, el)
            if img:
                blocks.append(f'<div class="block block--image" style="{style}">{img}</div>')
        elif ref.kind == "graphic_frame":
            gd = _graphic_data(el)
            uri = gd.get("uri") if gd is not None else None
            if uri and uri.endswith("/table"):
                table_html, plain = _render_table(ctx, gd)
                if plain.strip():
                    text_chunks.append(plain)
                blocks.append(f'<div class="block block--table" style="{style}">{table_html}</div>')
            elif uri and uri.endswith("/chart"):
                chart_html, plain = _render_chart(ctx, gd)
                if plain.strip():
                    text_chunks.append(plain)
                blocks.append(f'<div class="block block--chart" style="{style}">{chart_html}</div>')

    return "\n".join(blocks), "\n".join(text_chunks)


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------


def _line_spacing(pPr, profile: TemplateProfile) -> float:
    if pPr is not None:
        ln_spc = pPr.find(qn("a:lnSpc"))
        if ln_spc is not None:
            pct = ln_spc.find(qn("a:spcPct"))
            if pct is not None and pct.get("val"):
                return int(pct.get("val")) / 100000
    return profile.type_scale.body_line_spacing or 1.0


def _bullet_char(pPr) -> str | None:
    if pPr is None:
        return None
    if pPr.find(qn("a:buNone")) is not None:
        return None
    bu_char = pPr.find(qn("a:buChar"))
    if bu_char is not None:
        return bu_char.get("char") or "•"
    if pPr.find(qn("a:buAutoNum")) is not None:
        return "#"  # нумерованный список — реальный номер даёт CSS-счётчик <ol>
    return None


def _render_run(ctx: _Ctx, run) -> str:
    text = run.findtext(qn("a:t")) or ""
    if text == "":
        return ""
    rpr = run.find(qn("a:rPr"))
    size_pt = ctx.profile.type_scale_pt("body") or 18.0
    bold = False
    italic = False
    color_hex = ctx.profile.palette_roles.get("ink", _FALLBACK_INK)
    family = ctx.profile.type_scale.families[0] if ctx.profile.type_scale.families else "sans-serif"
    if rpr is not None:
        sz = rpr.get("sz")
        if sz:
            size_pt = int(sz) / 100
        bold = rpr.get("b") == "1"
        italic = rpr.get("i") == "1"
        fill = rpr.find(qn("a:solidFill"))
        resolved = resolve_color(fill, ctx.scheme, ctx.clr_map) if fill is not None else None
        if isinstance(resolved, Color):
            color_hex = resolved.hex
        latin = rpr.find(qn("a:latin"))
        if latin is not None and latin.get("typeface"):
            family = latin.get("typeface")
    px = round(size_pt * PX_PER_PT, 2)
    style = (
        f"font-size:{px}px;font-weight:{700 if bold else 400};"
        f"font-style:{'italic' if italic else 'normal'};color:{color_hex};"
        f"font-family:'{_esc(family)}',var(--font-fallback);"
    )
    return f'<span style="{style}">{_esc(text)}</span>'


def _render_text_shape(ctx: _Ctx, txbody) -> tuple[str, str]:
    body_pr = txbody.find(qn("a:bodyPr"))
    anchor = body_pr.get("anchor") if body_pr is not None else None
    justify = _ANCHOR_JUSTIFY.get(anchor, "flex-start")

    items_html: list[str] = []
    plain_parts: list[str] = []
    open_list_tag: str | None = None  # "ul" | "ol" | None

    def _close_list():
        nonlocal open_list_tag
        if open_list_tag is not None:
            items_html.append(f"</{open_list_tag}>")
            open_list_tag = None

    for p in txbody.findall(qn("a:p")):
        runs = p.findall(qn("a:r"))
        run_text = "".join(r.findtext(qn("a:t")) or "" for r in runs)
        if not run_text.strip():
            continue  # декоративный пустой абзац (см. докстроку _txbody_has_text)

        pPr = p.find(qn("a:pPr"))
        align = _ALIGN_CSS.get(pPr.get("algn") if pPr is not None else None, "")
        if not align:
            align = _ALIGN_CSS.get(ctx.profile.type_scale.default_align, "left")
        line_height = _line_spacing(pPr, ctx.profile)
        spans = "".join(_render_run(ctx, r) for r in runs)
        plain_parts.append(run_text)

        bullet = _bullet_char(pPr)
        p_style = f"text-align:{align};line-height:{line_height};"
        if bullet == "#":
            if open_list_tag != "ol":
                _close_list()
                items_html.append('<ol class="native-list">')
                open_list_tag = "ol"
            items_html.append(f'<li style="{p_style}">{spans}</li>')
        elif bullet:
            if open_list_tag != "ul":
                _close_list()
                items_html.append(f'<ul class="native-list" style="--bullet: \'{_esc(bullet)}\'">')
                open_list_tag = "ul"
            items_html.append(f'<li style="{p_style}">{spans}</li>')
        else:
            _close_list()
            items_html.append(f'<p style="{p_style}">{spans}</p>')

    _close_list()
    html_out = f'<div class="text-frame" style="justify-content:{justify}">{"".join(items_html)}</div>'
    return html_out, "\n".join(plain_parts)


# ---------------------------------------------------------------------------
# Заливки/линии автофигур
# ---------------------------------------------------------------------------


def _resolve_fill_hex(container, ctx: _Ctx) -> tuple[str | None, float]:
    if container is None:
        return None, 1.0
    fill = container.find(qn("a:solidFill"))
    if fill is None:
        return None, 1.0
    resolved = resolve_color(fill, ctx.scheme, ctx.clr_map)
    if isinstance(resolved, Color):
        return resolved.hex, resolved.alpha
    return None, 1.0


def _gradient_css(container, ctx: _Ctx) -> str | None:
    """`background: linear-gradient(...)` из `a:gradFill` — декоративные
    градиентные плашки (карточки/подложки) частые во всех трёх учебных
    шаблонах (см. отчёт задачи: без этого HTML честный, но заметно
    беднее самого `.pptx` на таких слайдах). Не путать с полной вёрностью
    OOXML-градиента (форма пути `a:path`, несколько остановок с
    произвольными модификаторами) — только линейный градиент по двум-трём
    точкам `a:gsLst`, этого достаточно для визуального сходства."""
    if container is None:
        return None
    grad = container.find(qn("a:gradFill"))
    if grad is None:
        return None
    gs_lst = grad.find(qn("a:gsLst"))
    if gs_lst is None:
        return None
    stops: list[tuple[float, str, float]] = []
    for gs in gs_lst.findall(qn("a:gs")):
        pos_raw = gs.get("pos")
        pos = (int(pos_raw) / 100000) if pos_raw is not None else 0.0
        color_el = next(iter(gs), None)
        resolved = resolve_color(color_el, ctx.scheme, ctx.clr_map) if color_el is not None else None
        if isinstance(resolved, Color):
            stops.append((pos, resolved.hex, resolved.alpha))
    if len(stops) < 2:
        return None
    stops.sort(key=lambda s: s[0])

    angle_deg = 90.0  # сверху вниз — фолбэк OOXML по умолчанию (a:lin без @ang не встречается на практике)
    lin = grad.find(qn("a:lin"))
    if lin is not None and lin.get("ang") is not None:
        ooxml_deg = int(lin.get("ang")) / 60000
        angle_deg = (ooxml_deg + 90) % 360  # OOXML 0°=вправо/по часовой → CSS 0°=вверх/по часовой
    stops_css = ", ".join(
        f"rgba({int(c[1:3],16)},{int(c[3:5],16)},{int(c[5:7],16)},{a:.3f}) {p*100:.1f}%"
        for p, c, a in stops
    )
    return f"background:linear-gradient({angle_deg:.1f}deg, {stops_css});"


def _shape_fill_css(ctx: _Ctx, el) -> str:
    sp_pr = el.find(qn("p:spPr"))
    if sp_pr is None:
        return ""
    css = ""
    hex_, alpha = _resolve_fill_hex(sp_pr, ctx)
    if hex_ is not None:
        css = f"background:{hex_};"
        if alpha < 1.0:
            css += f"opacity:{alpha:.3f};"
    else:
        gradient = _gradient_css(sp_pr, ctx)
        if gradient:
            css = gradient
    if not css:
        return ""
    prst_geom = sp_pr.find(qn("a:prstGeom"))
    prst = prst_geom.get("prst") if prst_geom is not None else "rect"
    if prst in ("ellipse", "roundRect", "round2SameRect", "round2DiagRect"):
        css += "border-radius:999px;" if prst == "ellipse" else "border-radius:12px;"
    return css


def _connector_css(ctx: _Ctx, el) -> str:
    sp_pr = el.find(qn("p:spPr"))
    ln = sp_pr.find(qn("a:ln")) if sp_pr is not None else None
    if ln is None:
        return ""
    hex_, alpha = _resolve_fill_hex(ln, ctx)
    if hex_ is None:
        return ""
    width_emu = ln.get("w")
    width_px = max(1.0, (int(width_emu) / 914400) * 96) if width_emu else 1.0
    return f"background:{hex_};opacity:{alpha:.3f};height:{width_px:.1f}px;"


# ---------------------------------------------------------------------------
# Картинки
# ---------------------------------------------------------------------------

_IMG_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".svg": "image/svg+xml",
    ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff",
}


def _render_picture(ctx: _Ctx, el) -> str | None:
    blip_fill = el.find(qn("p:blipFill"))
    blip = blip_fill.find(qn("a:blip")) if blip_fill is not None else None
    if blip is None:
        return None
    rid = blip.get(qn("r:embed"))
    if not rid:
        return None
    media_part = ctx.pkg.rels(ctx.slide_part).get(rid)
    if not media_part:
        return None
    ext = Path(media_part).suffix.lower()
    mime = _IMG_MIME.get(ext)
    if mime is None or mime == "image/svg+xml":
        return None  # неподдерживаемый/векторный формат — честно пропускаем, не роняем экспорт
    try:
        data = ctx.pkg.part(media_part)
    except KeyError:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f'<img src="data:{mime};base64,{b64}" alt="" loading="lazy">'


# ---------------------------------------------------------------------------
# Таблицы
# ---------------------------------------------------------------------------


def _render_table(ctx: _Ctx, gd) -> tuple[str, str]:
    tbl = gd.find(qn("a:tbl"))
    if tbl is None:
        return "", ""
    rows_html = []
    plain_parts = []
    for r_idx, tr in enumerate(tbl.findall(qn("a:tr"))):
        cells = []
        for tc in tr.findall(qn("a:tc")):
            text_html, plain, style = _render_table_cell(ctx, tc)
            tag = "th" if r_idx == 0 else "td"
            cells.append(f'<{tag} style="{style}">{text_html}</{tag}>')
            if plain.strip():
                plain_parts.append(plain)
        rows_html.append(f"<tr>{''.join(cells)}</tr>")
    html_out = f'<table class="native-table">{"".join(rows_html)}</table>'
    return html_out, "\n".join(plain_parts)


def _render_table_cell(ctx: _Ctx, tc) -> tuple[str, str, str]:
    txbody = tc.find(qn("a:txBody"))
    paragraphs = []
    plain_parts = []
    for p in txbody.findall(qn("a:p")) if txbody is not None else []:
        spans = "".join(_render_run(ctx, r) for r in p.findall(qn("a:r")))
        text = "".join(r.findtext(qn("a:t")) or "" for r in p.findall(qn("a:r")))
        if text.strip():
            plain_parts.append(text)
        paragraphs.append(spans)
    text_html = "<br>".join(p for p in paragraphs if p) or "&nbsp;"

    tc_pr = tc.find(qn("a:tcPr"))
    style = ""
    if tc_pr is not None:
        hex_, alpha = _resolve_fill_hex(tc_pr, ctx)
        if hex_:
            style = f"background:{hex_};"
            if alpha < 1.0:
                style += f"opacity:{alpha:.3f};"
    return text_html, "\n".join(plain_parts), style


# ---------------------------------------------------------------------------
# Графики — инлайновый SVG по данным реального c:chart (не растр)
# ---------------------------------------------------------------------------

_CHART_TAGS = ("barChart", "lineChart", "areaChart", "pieChart", "doughnutChart", "scatterChart")


def _chart_series_name(ser) -> str:
    tx = ser.find(qn("c:tx"))
    if tx is None:
        return ""
    v = tx.find(f".//{qn('c:v')}")
    return v.text or "" if v is not None else ""


def _chart_numeric_points(container_tag_el) -> list[float]:
    if container_tag_el is None:
        return []
    values = []
    for pt in container_tag_el.findall(f".//{qn('c:pt')}"):
        v = pt.find(qn("c:v"))
        try:
            values.append(float(v.text) if v is not None and v.text else 0.0)
        except ValueError:
            values.append(0.0)
    return values


def _chart_category_labels(cat_el) -> list[str]:
    if cat_el is None:
        return []
    out = []
    for pt in cat_el.findall(f".//{qn('c:pt')}"):
        v = pt.find(qn("c:v"))
        out.append((v.text or "") if v is not None else "")
    return out


def _chart_color_of(spPr, ctx: _Ctx) -> str | None:
    if spPr is None:
        return None
    fill = spPr.find(qn("a:solidFill"))
    resolved = resolve_color(fill, ctx.scheme, ctx.clr_map) if fill is not None else None
    return resolved.hex if isinstance(resolved, Color) else None


def _chart_point_colors(ser, ctx: _Ctx, n: int, fallback: str) -> list[str]:
    colors = [fallback] * n
    for dpt in ser.findall(qn("c:dPt")):
        idx_el = dpt.find(qn("c:idx"))
        if idx_el is None or idx_el.get("val") is None:
            continue
        idx = int(idx_el.get("val"))
        if 0 <= idx < n:
            color = _chart_color_of(dpt.find(qn("c:spPr")), ctx)
            if color:
                colors[idx] = color
    return colors


def _render_chart(ctx: _Ctx, gd) -> tuple[str, str]:
    chart_ref = gd.find(qn("c:chart"))
    if chart_ref is None:
        return "", ""
    rid = chart_ref.get(qn("r:id"))
    chart_part = ctx.pkg.rels(ctx.slide_part).get(rid) if rid else None
    if not chart_part:
        return _chart_fallback(), ""
    chart_root = ctx.pkg.xml(chart_part)

    kind_tag = None
    kind_node = None
    for tag in _CHART_TAGS:
        node = chart_root.find(f".//{qn('c:' + tag)}")
        if node is not None:
            kind_tag, kind_node = tag, node
            break
    if kind_node is None:
        return _chart_fallback(), ""

    palette = ctx.profile.chart_series or ["#0077FF", "#00D3E6", "#FF6B4A", "#8A5CF6"]
    series_data: list[tuple[str, list[float], list[str]]] = []
    categories: list[str] = []
    for s_idx, ser in enumerate(kind_node.findall(qn("c:ser"))):
        name = _chart_series_name(ser)
        values = _chart_numeric_points(ser.find(qn("c:val")))
        cats = _chart_category_labels(ser.find(qn("c:cat")))
        if not categories and cats:
            categories = cats
        series_color = _chart_color_of(ser.find(qn("c:spPr")), ctx) or palette[s_idx % len(palette)]
        point_colors = _chart_point_colors(ser, ctx, len(values), series_color)
        series_data.append((name or f"Ряд {s_idx + 1}", values, point_colors))

    plain = _chart_plain_text(categories, series_data)
    if kind_tag in ("pieChart", "doughnutChart"):
        svg = _svg_pie(categories, series_data, doughnut=kind_tag == "doughnutChart")
    elif kind_tag == "scatterChart":
        svg = _svg_scatter(series_data)
    elif kind_tag in ("lineChart", "areaChart"):
        svg = _svg_line(categories, series_data, filled=kind_tag == "areaChart")
    else:
        horizontal = kind_node.find(qn("c:barDir"))
        is_h = horizontal is not None and horizontal.get("val") == "bar"
        stacked = kind_node.find(qn("c:grouping"))
        is_stacked = stacked is not None and stacked.get("val") == "stacked"
        svg = _svg_bar(categories, series_data, horizontal=is_h, stacked=is_stacked)

    # Скрытая, но реальная в DOM таблица данных — числа/подписи находимы
    # поиском браузера и выделяются, даже если конкретный тип графика SVG
    # выше упростил геометрию (совпадает с философией "не картинка": важна
    # не пиксельная точность, а то, что содержание — текст, а не растр).
    data_table = _chart_data_table(categories, series_data)
    return f'<div class="chart-wrap">{svg}{data_table}</div>', plain


def _chart_fallback() -> str:
    return '<div class="chart-wrap chart-wrap--fallback">график</div>'


def _chart_plain_text(categories: list[str], series: list[tuple[str, list[float], list[str]]]) -> str:
    parts = list(categories)
    for name, values, _ in series:
        parts.append(name)
        parts.extend(_fmt_num(v) for v in values)
    return " ".join(parts)


def _fmt_num(v: float) -> str:
    return f"{v:g}"


def _chart_data_table(categories: list[str], series: list[tuple[str, list[float], list[str]]]) -> str:
    head = "<th></th>" + "".join(f"<th>{_esc(c)}</th>" for c in categories)
    rows = []
    for name, values, _ in series:
        cells = "".join(f"<td>{_esc(_fmt_num(v))}</td>" for v in values)
        rows.append(f"<tr><th>{_esc(name)}</th>{cells}</tr>")
    return f'<table class="chart-data sr-only"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _svg_bar(categories, series, *, horizontal: bool, stacked: bool) -> str:
    n_cat = max((len(v) for _, v, _ in series), default=0)
    if n_cat == 0:
        return _chart_fallback()
    if stacked:
        totals = [sum(s[1][i] for s in series if i < len(s[1])) for i in range(n_cat)]
        max_v = max(totals) or 1.0
    else:
        max_v = max((v for _, values, _ in series for v in values), default=1.0) or 1.0

    W, H, PAD = 400, 240, 24
    plot_w, plot_h = W - 2 * PAD, H - 2 * PAD
    n_series = max(len(series), 1)
    group_w = plot_w / max(n_cat, 1)
    bars = []
    labels = []
    for i in range(n_cat):
        stack_base = 0.0
        for s_i, (name, values, colors) in enumerate(series):
            v = values[i] if i < len(values) else 0.0
            frac = v / max_v if max_v else 0.0
            if stacked:
                bw = group_w * 0.6
                bh = frac * plot_h
                bx = PAD + i * group_w + (group_w - bw) / 2
                by = H - PAD - (stack_base / max_v) * plot_h - bh
                stack_base += v
            else:
                bw = (group_w * 0.7) / n_series
                bh = frac * plot_h
                bx = PAD + i * group_w + (group_w * 0.15) + s_i * bw
                by = H - PAD - bh
            color = colors[i] if i < len(colors) else "#0077FF"
            if horizontal:
                bars.append(
                    f'<rect x="{PAD}" y="{by - (H - 2*PAD - by):.1f}" width="{bh:.1f}" height="{bw:.1f}" fill="{color}"/>'
                )
            else:
                bars.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{color}"/>')
                bars.append(
                    f'<text x="{bx + bw/2:.1f}" y="{by - 4:.1f}" class="chart-label" text-anchor="middle">{_esc(_fmt_num(v))}</text>'
                )
        cat = categories[i] if i < len(categories) else ""
        labels.append(
            f'<text x="{PAD + i * group_w + group_w/2:.1f}" y="{H - PAD + 14:.1f}" class="chart-axis" text-anchor="middle">{_esc(cat)}</text>'
        )
    body = "".join(bars) + "".join(labels)
    return f'<svg viewBox="0 0 {W} {H}" class="chart-svg" role="img">{body}</svg>'


def _svg_line(categories, series, *, filled: bool) -> str:
    n_cat = max((len(v) for _, v, _ in series), default=0)
    if n_cat < 1:
        return _chart_fallback()
    max_v = max((v for _, values, _ in series for v in values), default=1.0) or 1.0
    min_v = min((v for _, values, _ in series for v in values), default=0.0)
    min_v = min(min_v, 0.0)
    span = (max_v - min_v) or 1.0

    W, H, PAD = 400, 240, 24
    plot_w, plot_h = W - 2 * PAD, H - 2 * PAD
    step = plot_w / max(n_cat - 1, 1)
    parts = []
    for name, values, colors in series:
        color = colors[0] if colors else "#0077FF"
        pts = [
            (PAD + i * step, H - PAD - ((v - min_v) / span) * plot_h)
            for i, v in enumerate(values)
        ]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if filled:
            floor_y = H - PAD - ((0 - min_v) / span) * plot_h
            poly = f"{PAD:.1f},{floor_y:.1f} {path} {PAD + (len(pts)-1)*step:.1f},{floor_y:.1f}"
            parts.append(f'<polygon points="{poly}" fill="{color}" opacity="0.35"/>')
        parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        for (x, y), v in zip(pts, values):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{color}"/>')
            parts.append(f'<text x="{x:.1f}" y="{y - 6:.1f}" class="chart-label" text-anchor="middle">{_esc(_fmt_num(v))}</text>')
    for i, cat in enumerate(categories[:n_cat]):
        x = PAD + i * step
        parts.append(f'<text x="{x:.1f}" y="{H - PAD + 14:.1f}" class="chart-axis" text-anchor="middle">{_esc(cat)}</text>')
    return f'<svg viewBox="0 0 {W} {H}" class="chart-svg" role="img">{"".join(parts)}</svg>'


def _svg_pie(categories, series, *, doughnut: bool) -> str:
    if not series:
        return _chart_fallback()
    name, values, colors = series[0]
    total = sum(values) or 1.0
    W = H = 260
    cx, cy, r = W / 2, H / 2, 100
    parts = []
    angle = -pi / 2
    for i, v in enumerate(values):
        frac = v / total
        sweep = frac * 2 * pi
        x0, y0 = cx + r * cos(angle), cy + r * sin(angle)
        end = angle + sweep
        x1, y1 = cx + r * cos(end), cy + r * sin(end)
        large = 1 if sweep > pi else 0
        color = colors[i] if i < len(colors) else "#0077FF"
        parts.append(
            f'<path d="M{cx:.1f},{cy:.1f} L{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f} Z" fill="{color}"/>'
        )
        mid = (angle + end) / 2
        lx, ly = cx + (r * 0.65) * cos(mid), cy + (r * 0.65) * sin(mid)
        cat = categories[i] if i < len(categories) else ""
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" class="chart-label" text-anchor="middle">{_esc(cat)} {frac*100:.0f}%</text>')
        angle = end
    if doughnut:
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r*0.55:.1f}" fill="var(--color-surface)"/>')
    return f'<svg viewBox="0 0 {W} {H}" class="chart-svg" role="img">{"".join(parts)}</svg>'


def _svg_scatter(series) -> str:
    all_points = [(v, i) for _, values, _ in series for i, v in enumerate(values)]
    if not all_points:
        return _chart_fallback()
    W, H, PAD = 400, 240, 24
    max_v = max((v for _, values, _ in series for v in values), default=1.0) or 1.0
    parts = []
    for s_i, (name, values, colors) in enumerate(series):
        color = colors[0] if colors else "#0077FF"
        n = len(values)
        for i, v in enumerate(values):
            x = PAD + (i / max(n - 1, 1)) * (W - 2 * PAD)
            y = H - PAD - (v / max_v) * (H - 2 * PAD)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
    return f'<svg viewBox="0 0 {W} {H}" class="chart-svg" role="img">{"".join(parts)}</svg>'


# ---------------------------------------------------------------------------
# Шрифты — вшиваются из системы, если реально установлены (см. докстроку модуля)
# ---------------------------------------------------------------------------

_FC_MATCH = shutil.which("fc-match")


def _system_font_path(family: str) -> Path | None:
    """Путь к файлу шрифта `family`, ТОЛЬКО если `fontconfig` реально нашёл
    именно эту гарнитуру (не приблизительный фолбэк вроде Verdana на
    незнакомое имя) — сравнение по названию семейства, а не по факту, что
    `fc-match` вообще что-то вернул (fontconfig возвращает совпадение
    всегда, даже на случайное имя)."""
    if _FC_MATCH is None:
        return None
    try:
        result = subprocess.run(
            [_FC_MATCH, "-f", "%{file}\t%{family}\n", family],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    line = result.stdout.strip().splitlines()[0]
    if "\t" not in line:
        return None
    path_str, matched_family = line.split("\t", 1)
    matched_names = matched_family.lower()
    if family.lower().split()[0] not in matched_names:
        return None
    path = Path(path_str)
    return path if path.exists() else None


_FONT_MIME = {".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection"}


def _embed_fonts(profile: TemplateProfile) -> tuple[str, list[str]]:
    warnings: list[str] = []
    blocks: list[str] = []
    seen: set[str] = set()
    for family in profile.type_scale.families:
        if family in seen:
            continue
        seen.add(family)
        path = _system_font_path(family)
        if path is None:
            warnings.append(
                f"гарнитура шаблона {family!r} не найдена среди установленных в систему шрифтов — "
                "в HTML подставлен запасной sans-serif, файл не искажён, но выглядит не как в шаблоне."
            )
            continue
        mime = _FONT_MIME.get(path.suffix.lower(), "font/ttf")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        blocks.append(
            f"@font-face{{font-family:'{family}';src:url(data:{mime};base64,{b64}) "
            f"format('{'opentype' if path.suffix.lower()=='.otf' else 'truetype'}');"
            f"font-weight:400 700;font-display:swap;}}"
        )
        bold_path = _system_font_path(f"{family}:style=Bold")
        if bold_path is not None and bold_path != path:
            b64b = base64.b64encode(bold_path.read_bytes()).decode("ascii")
            mime_b = _FONT_MIME.get(bold_path.suffix.lower(), "font/ttf")
            blocks.append(
                f"@font-face{{font-family:'{family}';src:url(data:{mime_b};base64,{b64b}) "
                f"format('{'opentype' if bold_path.suffix.lower()=='.otf' else 'truetype'}');"
                f"font-weight:700;font-display:swap;}}"
            )
    return "\n".join(blocks), warnings


# ---------------------------------------------------------------------------
# Документ целиком
# ---------------------------------------------------------------------------


def _score_badge_html(score: dict | None) -> str:
    """Задача G: бейдж с оценками PPTEval этого слайда (content/design,
    1-5) в углу слайда — необязательный, как и сама оценка (см. докстроку
    `_parse_scores` в `audit.visual`: слайд без валидной оценки просто не
    попадает в `slide_scores`, `score` тогда `None` или без нужных ключей).
    `why` — не в подписи (одна фраза может быть длинной, ломает бейдж), а в
    `title` — всплывающая подсказка браузера при наведении."""
    if not score:
        return ""
    parts = []
    if isinstance(score.get("content"), int):
        parts.append(f"С {score['content']}")
    if isinstance(score.get("design"), int):
        parts.append(f"Д {score['design']}")
    if not parts:
        return ""
    why = score.get("why")
    title_attr = f' title="{_esc(why)}"' if isinstance(why, str) and why else ""
    return f'<div class="slide-score"{title_attr}>{_esc(" · ".join(parts))}</div>'


def _slide_section(
    index: int, blocks_html: str, bg_css: str, is_dark: bool, spec_slide: SlideSpec | None,
    score: dict | None = None,
) -> str:
    kind = spec_slide.kind if spec_slide is not None else ""
    title = spec_slide.headline if spec_slide is not None else f"Слайд {index + 1}"
    theme = "dark" if is_dark else "light"
    # Текст докладчика едет рядом со слайдом, а не только на странице заметок
    # .pptx (уточнение заказчика 23 сентября 2026: на выходе ждут «готовые
    # слайды и текст к каждому слайду»). Лежит в разметке всегда, показывается
    # по кнопке — печатать его поверх слайда нельзя, это текст для
    # рассказывающего, а не для зала.
    notes = getattr(spec_slide, "speaker_notes", None) if spec_slide is not None else None
    notes_html = f'<aside class="slide-notes">{_esc(notes.strip())}</aside>' if notes and notes.strip() else ""
    score_html = _score_badge_html(score)
    return (
        f'<section class="slide" data-index="{index}" data-kind="{_esc(kind)}" '
        f'data-theme="{theme}" aria-label="{_esc(title)}" style="background:{bg_css};">'
        f'<div class="slide-inner">{blocks_html}</div>{notes_html}{score_html}'
        f"</section>"
    )


def _css_vars(profile: TemplateProfile) -> str:
    roles = profile.palette_roles
    lines = [f"--color-{role}: {hexv};" for role, hexv in sorted(roles.items())]
    lines.append(f"--color-ink: {roles.get('ink', _FALLBACK_INK)};")
    lines.append(f"--color-surface: {roles.get('surface', _FALLBACK_SURFACE)};")
    lines.append(f"--color-brand: {roles.get('brand', _FALLBACK_BRAND)};")
    families = profile.type_scale.families or ["sans-serif"]
    lines.append(f"--font-primary: '{families[0]}';")
    if len(families) > 1:
        lines.append(f"--font-secondary: '{families[1]}';")
    lines.append("--font-fallback: system-ui, -apple-system, 'Segoe UI', sans-serif;")
    return "\n".join(lines)


def _deck_score_html(deck_score: dict | None, content_avg: float | None, design_avg: float | None) -> str:
    """Задача G: сводка PPTEval по колоде целиком — средние по content/
    design (считает `audit.visual._axis_average`, не эта функция — export
    только показывает готовое число) плюс coherence с колоды. Пустая
    строка, если оценок нет вовсе (аудит не запускался/не передан) — тот
    же принцип честной деградации, что и у `HtmlExportResult.warnings`."""
    parts = []
    if content_avg is not None:
        parts.append(f"содержание {content_avg:.1f}")
    if design_avg is not None:
        parts.append(f"дизайн {design_avg:.1f}")
    coherence = deck_score.get("coherence") if deck_score else None
    if isinstance(coherence, int):
        parts.append(f"связность {coherence}")
    if not parts:
        return ""
    why = deck_score.get("why") if deck_score else None
    title_attr = f' title="{_esc(why)}"' if isinstance(why, str) and why else ""
    return f'<div id="deck-score"{title_attr}>PPTEval: {_esc(", ".join(parts))}</div>'


def _run_budget_html(budget: dict | None, risky_slides: dict[int, dict] | None) -> str:
    """Задача L: режим прогона, секунды по стадиям и слайды, ушедшие на
    аудит по картинке (с обоими рисками) — тот же принцип честной
    видимости, что и у `_deck_score_html` (пустая строка, если бюджета нет,
    не только CSS прячет отсутствующее)."""
    if not budget:
        return ""
    mode = budget.get("mode")
    if not mode:
        return ""
    checkpoint = budget.get("mode_checkpoint")
    stage_seconds = budget.get("stage_seconds") or {}
    stages_txt = ", ".join(f"{name} {seconds:.0f}с" for name, seconds in stage_seconds.items())
    mode_txt = f"режим {mode}" + (f" (точка «{checkpoint}»)" if checkpoint else "")

    risky_txt = ""
    if risky_slides:
        items = sorted(risky_slides.items(), key=lambda kv: int(kv[0]))
        risky_txt = "; аудит по картинке: " + ", ".join(
            f"слайд {pos} (техн. {risk['technical']:.1f}, смысл. {risk['semantic']:.1f})"
            for pos, risk in items
        )

    # Задача W: сколько раз звали модель и сколько ждали общую очередь,
    # что пропущено по времени и что сделано запасным путём на потолке.
    calls_txt = ""
    if budget.get("model_calls"):
        calls_txt = (
            f"; вызовов модели {budget['model_calls']}, "
            f"ожидание очереди {budget.get('queue_wait_seconds', 0):.0f}с"
        )
    skipped = budget.get("time_skipped") or {}
    skipped_txt = ""
    if skipped:
        skipped_txt = "; пропущено по времени: " + ", ".join(
            f"{what} ×{count}" if count > 1 else what for what, count in skipped.items()
        )
    warnings = budget.get("warnings") or []
    warnings_txt = ("; потолок бюджета: " + "; ".join(warnings)) if warnings else ""

    text = (
        mode_txt + ((" · " + stages_txt) if stages_txt else "") + calls_txt + skipped_txt + warnings_txt
        + risky_txt
    )
    return f'<div id="run-budget">{_esc(text)}</div>'


def _fidelity_html(fidelity: "FidelityReport | None") -> str:
    """Задача T: блок «Верность шаблону» (`audit.fidelity.FidelityReport`) —
    отдельная функция, а не правка `_deck_score_html`/`_run_budget_html`,
    намеренно: параллельная задача M добавляет в тот же отчёт свой блок
    (оценки аудита по картинке), и оба блока не должны задевать один и тот
    же код. Тот же принцип честной видимости, что и у соседних блоков —
    пустая строка, если отчёт не передан (старый вызывающий код/тесты не
    обязаны знать про эту задачу)."""
    if fidelity is None:
        return ""
    return f'<div id="template-fidelity" title="{_esc(fidelity.summary)}">{_esc(fidelity.summary)}</div>'


# Ступени лестницы сборки (`compose.builder.LADDER_RUNGS`) в порядке
# лестницы, подписи для отчёта. Своя копия: экспорту незачем тянуть сборщик.
_LADDER_LABELS = (
    ("clone", "клон"), ("adapt", "запасная раскладка"), ("shorten", "сокращение текста"),
    ("split", "разбиение"), ("scratch", "с нуля"),
)


def _ladder_html(deck_spec: DeckSpec) -> str:
    """Задача U: сколько слайдов какой ступенью лестницы сборки собрано
    (`DeckSpec.meta["ladder_*"]`, пишет `build_deck`). Пусто, если колоду
    собирали без лестницы (старый план, тесты экспорта)."""
    counts = [(label, deck_spec.meta.get(f"ladder_{key}")) for key, label in _LADDER_LABELS]
    if all(value is None for _label, value in counts):
        return ""
    text = "Лестница сборки: " + ", ".join(f"{label} {value or 0}" for label, value in counts)
    return f'<div id="build-ladder">{_esc(text)}</div>'


def _wrap_document(
    deck_spec: DeckSpec, profile: TemplateProfile, canvas: Canvas, slides_html: list[str], fonts_css: str,
    *, deck_score: dict | None = None, content_avg: float | None = None, design_avg: float | None = None,
    budget: dict | None = None, risky_slides: dict[int, dict] | None = None,
    fidelity: "FidelityReport | None" = None,
) -> str:
    width_px = round(canvas.width_in * 96)
    height_px = round(canvas.height_in * 96)
    css_vars = _css_vars(profile)
    slides_joined = "\n".join(slides_html)
    n_slides = len(slides_html)
    lang = (deck_spec.language or "ru")[:2]
    deck_score_html = _deck_score_html(deck_score, content_avg, design_avg)
    run_budget_html = _run_budget_html(budget, risky_slides)
    fidelity_html = _fidelity_html(fidelity)
    ladder_html = _ladder_html(deck_spec)

    return f"""<!DOCTYPE html>
<html lang="{_esc(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(deck_spec.title)}</title>
<style>
{fonts_css}
:root {{
{css_vars}
--slide-w: {width_px}px;
--slide-h: {height_px}px;
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0; height: 100%;
  background: #1b1c1f; color: var(--color-ink);
  font-family: var(--font-primary), var(--font-fallback);
}}
#viewport {{
  position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}}
#stage {{
  position: relative; width: var(--slide-w); height: var(--slide-h);
  transform-origin: center center;
}}
.slide {{
  position: absolute; inset: 0; width: 100%; height: 100%;
  overflow: hidden; display: none;
  box-shadow: 0 8px 40px rgba(0,0,0,.35);
}}
.slide.is-current {{ display: block; }}
.slide-inner {{ position: absolute; inset: 0; }}
/* Текст докладчика: в разметке всегда, на экране — по клавише N. Поверх
   слайда он не печатается никогда: это текст для рассказывающего, не для
   зала. В режиме обзора (`body.overview`) тоже скрыт — там миниатюры. */
.slide-notes {{ display: none; }}
body.notes .slide.is-current .slide-notes {{
  display: block; position: absolute; left: 0; right: 0; bottom: 0;
  max-height: 38%; overflow: auto; z-index: 5;
  padding: 14px 18px; box-sizing: border-box;
  background: rgba(17,17,17,.88); color: #fff;
  font: 400 15px/1.45 var(--font-fallback); white-space: pre-wrap;
}}
body.overview .slide-notes {{ display: none !important; }}
/* Задача G (PPTEval): бейдж с оценками слайда — угол слайда, не мешает
   контенту (шрифт мельче любого реального текста слайда). Пуст (нет
   элемента в DOM), если у слайда нет валидной оценки — см. `_score_badge_
   html`, не только CSS прячет отсутствующее. */
.slide-score {{
  position: absolute; right: 8px; top: 8px; z-index: 4;
  font: 600 11px var(--font-fallback); color: #fff;
  background: rgba(17,17,17,.72); padding: 2px 8px; border-radius: 999px;
  pointer-events: none;
}}
#deck-score {{
  display: none; position: fixed; left: 16px; top: 12px; z-index: 10;
  font: 13px var(--font-fallback); color: #fff; background: rgba(0,0,0,.55);
  padding: 4px 10px; border-radius: 999px;
}}
body.overview #deck-score {{ display: block; }}
/* Задача L: режим прогона/секунды по стадиям/слайды на аудите по картинке
   — под бейджем PPTEval, тем же приёмом (видим только в обзоре, пуст, если
   бюджет не передан). */
#run-budget {{
  display: none; position: fixed; left: 16px; top: 44px; z-index: 10;
  max-width: min(70vw, 640px);
  font: 12px var(--font-fallback); color: #fff; background: rgba(0,0,0,.55);
  padding: 4px 10px; border-radius: 12px;
}}
body.overview #run-budget {{ display: block; }}
/* Задача T: «Верность шаблону» — под режимом прогона, тот же приём (виден
   только в обзоре, пуст, если отчёт не передан). */
#template-fidelity {{
  display: none; position: fixed; left: 16px; top: 76px; z-index: 10;
  max-width: min(70vw, 640px);
  font: 12px var(--font-fallback); color: #fff; background: rgba(0,0,0,.55);
  padding: 4px 10px; border-radius: 12px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
body.overview #template-fidelity {{ display: block; }}
/* Задача U: счётчик ступеней лестницы сборки, под «Верностью шаблону». */
#build-ladder {{
  display: none; position: fixed; left: 16px; top: 108px; z-index: 10;
  max-width: min(70vw, 640px);
  font: 12px var(--font-fallback); color: #fff; background: rgba(0,0,0,.55);
  padding: 4px 10px; border-radius: 12px;
}}
body.overview #build-ladder {{ display: block; }}
.block {{ position: absolute; }}
.text-frame {{ position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: flex-start; }}
.text-frame p, .text-frame li {{ margin: 0 0 .25em 0; padding: 0; }}
.native-list {{ margin: 0; padding: 0; list-style: none; }}
.native-list li {{ position: relative; padding-left: 1.1em; }}
.native-list li::before {{ content: var(--bullet, '•'); position: absolute; left: 0; }}
ol.native-list {{ counter-reset: item; }}
ol.native-list li {{ counter-increment: item; }}
ol.native-list li::before {{ content: counter(item) '.'; }}
.block--image img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
.native-table {{ width: 100%; height: 100%; border-collapse: collapse; font-size: 1em; }}
.native-table td, .native-table th {{ border: 1px solid rgba(0,0,0,.08); padding: .3em .5em; text-align: left; }}
.chart-wrap {{ position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }}
.chart-svg {{ width: 100%; height: 100%; }}
.chart-label, .chart-axis {{ font-size: 9px; fill: var(--color-ink); font-family: var(--font-primary), var(--font-fallback); }}
.chart-wrap--fallback {{ color: var(--color-ink); opacity: .5; font-size: 14px; }}
.sr-only {{
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0,0,0,0); white-space: nowrap; border: 0; padding: 0; margin: -1px;
}}
#hud {{
  position: fixed; left: 16px; bottom: 12px; z-index: 10;
  font: 13px var(--font-fallback); color: #fff; background: rgba(0,0,0,.45);
  padding: 4px 10px; border-radius: 999px; user-select: none;
}}
#help {{
  position: fixed; right: 16px; bottom: 12px; z-index: 10;
  font: 12px var(--font-fallback); color: rgba(255,255,255,.7);
}}
body.overview #viewport {{ display: none; }}
#grid {{
  display: none; position: fixed; inset: 0; overflow: auto; padding: 24px;
  background: #1b1c1f;
}}
body.overview #grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
#grid .thumb {{
  position: relative; width: 100%; aspect-ratio: {width_px} / {height_px};
  overflow: hidden; cursor: pointer; outline: 2px solid transparent; border-radius: 4px;
}}
#grid .thumb.is-current {{ outline-color: var(--color-brand); }}
#grid .thumb .slide {{ display: block; position: absolute; inset: 0; }}
#grid .thumb .slide-scaler {{
  position: absolute; top: 0; left: 0; width: {width_px}px; height: {height_px}px;
  transform-origin: top left;
}}
#grid .thumb-index {{
  position: absolute; left: 4px; top: 4px; font: 11px var(--font-fallback);
  color: #fff; background: rgba(0,0,0,.5); padding: 1px 6px; border-radius: 999px; z-index: 2;
}}
@media print {{
  html, body {{ background: #fff; }}
  #hud, #help, #deck-score, #run-budget, #template-fidelity {{ display: none !important; }}
  body.overview #grid {{ display: none !important; }}
  #viewport {{ position: static; display: block; }}
  #stage {{ display: none; }}
  .print-pages {{ display: block; }}
  .print-pages .slide {{
    display: block; position: relative; width: 100%; aspect-ratio: {width_px} / {height_px};
    page-break-after: always; box-shadow: none;
  }}
}}
.print-pages {{ display: none; }}
</style>
</head>
<body>
<div id="viewport"><div id="stage">
{slides_joined}
</div></div>
{deck_score_html}
{run_budget_html}
{fidelity_html}
{ladder_html}
<div id="grid"></div>
<div class="print-pages" aria-hidden="true">
{slides_joined}
</div>
<div id="hud">1 / {n_slides}</div>
<div id="help">← → навигация · g обзор · n текст докладчика · Ctrl/⌘+P — печать в PDF</div>
<script>
(function() {{
  var slides = Array.prototype.slice.call(document.querySelectorAll('#stage .slide'));
  var hud = document.getElementById('hud');
  var stage = document.getElementById('stage');
  var grid = document.getElementById('grid');
  var current = 0;

  function show(i) {{
    current = Math.max(0, Math.min(slides.length - 1, i));
    slides.forEach(function(s, idx) {{ s.classList.toggle('is-current', idx === current); }});
    hud.textContent = (current + 1) + ' / ' + slides.length;
    var thumb = grid.querySelector('[data-thumb-index="' + current + '"]');
    if (thumb) {{
      grid.querySelectorAll('.thumb').forEach(function(t) {{ t.classList.remove('is-current'); }});
      thumb.classList.add('is-current');
    }}
  }}

  function fit() {{
    var vw = window.innerWidth, vh = window.innerHeight;
    var sw = {width_px}, sh = {height_px};
    var scale = Math.min(vw / sw, vh / sh) * 0.96;
    stage.style.transform = 'scale(' + scale + ')';
  }}

  function buildGrid() {{
    if (grid.childNodes.length) return;
    slides.forEach(function(s, idx) {{
      var thumb = document.createElement('div');
      thumb.className = 'thumb';
      thumb.setAttribute('data-thumb-index', String(idx));
      var badge = document.createElement('div');
      badge.className = 'thumb-index';
      badge.textContent = String(idx + 1);
      var scaler = document.createElement('div');
      scaler.className = 'slide-scaler';
      var clone = s.cloneNode(true);
      clone.classList.add('is-current');
      scaler.appendChild(clone);
      thumb.appendChild(badge);
      thumb.appendChild(scaler);
      thumb.addEventListener('click', function() {{
        show(idx);
        document.body.classList.remove('overview');
      }});
      grid.appendChild(thumb);
    }});
    fitGrid();
  }}

  function fitGrid() {{
    grid.querySelectorAll('.thumb').forEach(function(thumb) {{
      var w = thumb.clientWidth;
      var scale = w / {width_px};
      var scaler = thumb.querySelector('.slide-scaler');
      if (scaler) scaler.style.transform = 'scale(' + scale + ')';
    }});
  }}

  window.addEventListener('resize', function() {{ fit(); fitGrid(); }});
  document.addEventListener('keydown', function(ev) {{
    if (ev.key === 'ArrowRight' || ev.key === 'PageDown' || ev.key === ' ') {{ show(current + 1); ev.preventDefault(); }}
    else if (ev.key === 'ArrowLeft' || ev.key === 'PageUp') {{ show(current - 1); ev.preventDefault(); }}
    else if (ev.key === 'Home') {{ show(0); }}
    else if (ev.key === 'End') {{ show(slides.length - 1); }}
    else if (ev.key === 'g' || ev.key === 'G') {{
      document.body.classList.toggle('overview');
      if (document.body.classList.contains('overview')) {{ buildGrid(); fitGrid(); }}
    }}
    else if (ev.key === 'n' || ev.key === 'N') {{ document.body.classList.toggle('notes'); }}
    else if (ev.key === 'Escape') {{ document.body.classList.remove('overview'); document.body.classList.remove('notes'); }}
  }});

  fit();
  show(0);
}})();
</script>
</body>
</html>
"""
