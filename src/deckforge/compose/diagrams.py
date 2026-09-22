"""Схемы (process/cycle/hierarchy/funnel/comparison/timeline) и ряды
пиктограмм — нативными автофигурами и соединителями `python-pptx`, не
растром и не настоящим `dgm:` SmartArt.

## Почему не настоящий SmartArt

`dgm:` (DrawingML Diagram, ECMA-376 Part 1 §21.4) — отдельный, сложный
формат: граф данных (`data1.xml`) отдельно от layout-определения
(`layout1.xml`, XSLT-подобный язык раскладки) отдельно от применённого
рисунка (`p:graphicFrame/a:graphic/a:graphicData` с `uri` диаграммы), и сам
рисунок КЭШИРУЕТСЯ как `dsp:` drawingShape-дерево именно для программ,
которые не умеют раскладывать SmartArt заново. `python-pptx` не пишет и не
читает ни один из этих XML-словарей (нет ни `Diagram`-объекта, ни фабрики
для `data1.xml`/`layout1.xml`), а LibreOffice (обязательный рендерер этого
проекта, `soffice`) поддерживает SmartArt тоже не полностью — на слайде с
`dgm:`, собранном в обход `python-pptx` руками через сырой XML, экспорт в
PDF у LibreOffice либо теряет раскладку, либо разъезжается (бриф задачи
дословно). Раз оба звена конвейера (сборка и экспорт) не тянут формат
целиком, честная альтернатива — блок из нативных автофигур/соединителей: в
PowerPoint он редактируется КАК SmartArt (перетащить фигуру, перекрасить,
дописать текст — всё то же самое взаимодействие), но остаётся обычным
`p:sp`/`p:cxnSp`, который переживает экспорт в PDF без потерь, потому что
это ровно то, что LibreOffice и без того умеет рисовать.

## Геометрия схем — из словаря шаблона, не по вкусу

Карточки схем рисуются формой с НАИБОЛЬШИМ числом вхождений среди
`rect`/`roundRect`/`ellipse` в `profile.shape_vocabulary` (Task 7/10,
`template/shapes.py`) — у VK Tech это `rect` (1359 против 176 roundRect),
у Education — `ellipse` (100 против 27 roundRect). Степень скругления
`roundRect`, если он выбран, берётся из среднего фактического `adj`
шаблона (`ShapeVocabEntry.avg_adj`), а не из дефолта PowerPoint (16.67%) —
рисовать скруглённые карточки со скруглением, которого в шаблоне нет,
такой же выход из дизайн-системы, как сама форма.
"""
from __future__ import annotations
import io
import math
from dataclasses import dataclass
from pathlib import Path

from lxml import etree
from PIL import Image
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_CONNECTOR
from pptx.enum.text import PP_ALIGN
from pptx.util import Pt

from deckforge.compose.colorpick import (
    best_contrast_text_color, pop_pair_for_luminance, slide_background_luminance,
)
from deckforge.compose.textfit import measure
from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.template.profile import TemplateProfile
from deckforge.template.shapes import KNOWN_AUTOSHAPE_PRESETS

EMU_PER_INCH = 914400

DIAGRAM_KINDS = ("process", "cycle", "hierarchy", "funnel", "comparison", "timeline")

# Доля холста между соседними карточками схемы — тот же порядок величины,
# что `Grid.gutter` намайненных шаблонов (единицы процентов холста), не
# откалиброван отдельно под схемы.
_GUTTER = 0.02

# Доля площади подложки, которую занимает сама иконка (см. докстроку
# `add_pictogram_row` про контрастную подложку) — меньше 1.0, чтобы
# подложка оставалась видна КАЙМОЙ вокруг иконки, а не была полностью ею
# перекрыта; 0.62 — на глаз (типичный отступ иконки внутри бейджа в самих
# наборах шаблонов, см. кружки-бейджи VK Tech confidence=1.0), не измерено
# отдельно.
_ICON_INSET_SHARE = 0.62

# Ступени шкалы, по которым перебирается подпись карточки схемы — от
# крупной к мелкой; полного цикличного ужимания таблиц/укладки здесь нет
# (карточка схемы короче слота контента по замыслу), достаточно перебора
# по готовым ступеням шкалы.
_LABEL_STEPS = ("caption", "micro", "body")


@dataclass(frozen=True)
class DiagramSpec:
    kind: str
    items: list[str]


class NoIconSet(RuntimeError):
    """У шаблона нет иконочного набора (`profile.assets.icons` пуст) —
    `add_pictogram_row` отказывается подставлять чужой набор или эмодзи
    (бриф Task 10 дословно: "иконки либо из шаблона, либо их нет")."""


# ---------------------------------------------------------------------------
# Публичный интерфейс — схемы
# ---------------------------------------------------------------------------


def add_diagram(slide, box: Box, spec: DiagramSpec, profile: TemplateProfile) -> list:
    if spec.kind not in DIAGRAM_KINDS:
        raise ValueError(f"Неизвестный тип схемы {spec.kind!r} — ожидается один из {DIAGRAM_KINDS}")
    if not spec.items:
        raise ValueError("DiagramSpec.items пуст — схеме нечего рисовать")

    builder = _BUILDERS[spec.kind]
    return builder(slide, box, spec.items, profile)


# Живой замер на VK Tech (Task 10 отчёт, обязательный визуальный обзор):
# каталог иконок несёт `confidence` (assets.py — доля размещений, реально
# ведущих себя как иконка), и он расслаивается резко — 11 значений 1.0
# против 25 значений 0.4. Первые пять иконок каталога В ПОРЯДКЕ ОБХОДА
# ПАКЕТА (без сортировки по confidence) оказались декоративным мусором:
# голая буква "D" (823 байта), два почти пустых PNG на 141/167 байт
# (плейсхолдер / едва видимая искра) — совсем не пиктограммы. Сортировка по
# убыванию confidence вместо порядка каталога подняла наверх 11
# единообразных значков-бейджей (закруглённый квадрат с символом) — тот же
# принцип, что уже применяет сам каталог ассетов к другим категориям.
# Дополнительный фильтр по размеру файла — подстраховка НА СЛУЧАЙ, если у
# другого шаблона confidence не так резко расслаивается: все три очевидно
# пустых/бракованных файла VK Tech (141/167 байт) меньше порога, все
# проверенные настоящие пиктограммы (даже самый лёгкий, 526 байт) — больше;
# порог взят с запасом между двумя группами, не как измерение "минимального
# байта настоящей иконки" в общем случае.
_MIN_ICON_BYTES = 300


def add_pictogram_row(slide, box: Box, icons: list[str], profile: TemplateProfile) -> list:
    """Ряд пиктограмм из иконочного набора шаблона — `NoIconSet`, если у
    шаблона нет ни одной иконки (см. докстроку класса).

    Подбор — позиционный (по убыванию `confidence` каталога, см. `_MIN_
    ICON_BYTES` выше), не по имени файла и не по эмодзи: `profile` (Task
    10) пока не несёт текстовых описаний иконок (та часть Task 8, что
    должна была их собрать моделью — `VLM, одна картинка на иконку, кэш по
    md5` — в этом кодовом дереве не реализована, см. task-10-report.md,
    раздел про подбор пиктограмм) — подбор ПО СМЫСЛУ подписи `icons[i]`
    целенаправленно не реализован заглушкой "по факту это имя файла", а
    честно откладывается на момент, когда описания появятся; сам список
    источников (`profile.assets.icons`) и запрет на посторонние наборы/
    эмодзи соблюдены уже сейчас."""
    catalog = list(profile.assets.icons)
    if not catalog:
        raise NoIconSet(
            "У шаблона нет иконочного набора (profile.assets.icons пуст) — "
            "пиктограммы на слайде не рисуются (чужие наборы и эмодзи не подставляются)."
        )
    if not profile.source_path:
        raise NoIconSet(
            "У профиля не задан source_path — байты иконки шаблона взять неоткуда "
            "(профиль собран без пути к файлу, см. TemplateProfile.from_file)."
        )

    usable = [ref for ref in catalog if ref.size_bytes >= _MIN_ICON_BYTES]
    available = sorted(usable or catalog, key=lambda ref: (-ref.confidence, ref.part_name))

    n = len(icons)
    chosen = [available[i % len(available)] for i in range(n)]
    left, top, _width, height = _emu_box(box, profile)
    total_width = round(box.width * profile.canvas_width_emu)
    gutter = round(total_width * _GUTTER) if n > 1 else 0
    slot_width = (total_width - gutter * (n - 1)) // n if n else 0
    plate_size = max(1, min(slot_width, height))
    # Иконка внутри подложки — не во весь размер плашки, иначе плашка не
    # видна вовсе как контрастная рамка (см. докстроку про подложку ниже).
    icon_size = max(1, round(plate_size * _ICON_INSET_SHARE))

    # Task 10 отчёт (визуальное ревью, находки №5 и №6): у Education набор
    # иконок контурный в том же синем тоне, что фирменный фон шаблона —
    # линии иконок почти не видны на слайде. Цвет готового PNG нельзя
    # перекрасить, не трогая пиксели (перекраска произвольного PNG в
    # границах этой задачи не бралась) — вместо этого под каждую иконку
    # кладётся контрастная НЕПРОЗРАЧНАЯ подложка. Первая версия подбирала
    # ЕЁ цвет от фона СЛАЙДА (`pop_pair_for_luminance` от `slide`) — почти
    # всегда достаточно (иконка обычно окрашена "под фон", раз изначально
    # рисовалась для него), но на ЛЦТ2026 сломалось ровно симметрично:
    # иконки там сами БЕЛЫЕ, фон слайда тёмный → подложка выбралась белой
    # (контрастной ФОНУ) — и иконка снова пропала, теперь уже на подложке.
    # Верный контраст — не к фону слайда, а к САМОЙ ИКОНКЕ: подложка
    # подбирается по средней яркости непрозрачных пикселей КАЖДОЙ картинки
    # (`_icon_average_luminance`), гарантированно контрастна именно ей,
    # независимо от того, что за фон под ней. Форма подложки — из словаря
    # шаблона, предпочтение `ellipse` (бейдж-иконка в кружке — узнаваемый
    # паттерн), тот же принцип, что и `_marker_geometry` в "timeline".
    plate_prst, plate_type, plate_adj = _marker_geometry(profile)
    fallback_plate_fill, _text_hex = pop_pair_for_luminance(
        slide_background_luminance(slide, profile), profile.palette_roles,
    )

    # Подложки добавляются на слайд (видимы и редактируемы, как любая
    # другая нативная фигура), но в возвращаемый список не попадают —
    # интерфейс брифа `-> list[Shape]` про САМИ пиктограммы (вызывающему
    # коду нужен список именно картинок, например посчитать их или
    # переставить); подложка — деталь оформления, не отдельная пиктограмма.
    pictures: list = []
    with PptxPackage.open(Path(profile.source_path)) as pkg:
        for i, ref in enumerate(chosen):
            slot_x = left + i * (slot_width + gutter) + (slot_width - plate_size) // 2
            slot_y = top + (height - plate_size) // 2

            data = pkg.part(ref.part_name)
            icon_luminance = _icon_average_luminance(data)
            if icon_luminance is not None:
                plate_fill, _text_hex = pop_pair_for_luminance(icon_luminance, profile.palette_roles)
            else:
                plate_fill = fallback_plate_fill

            plate = slide.shapes.add_shape(plate_type, slot_x, slot_y, plate_size, plate_size)
            if plate_prst == "roundRect" and plate_adj > 0:
                try:
                    plate.adjustments[0] = plate_adj
                except (IndexError, ValueError):
                    pass
            plate.fill.solid()
            plate.fill.fore_color.rgb = _rgb(plate_fill)
            plate.line.fill.background()

            icon_x = slot_x + (plate_size - icon_size) // 2
            icon_y = slot_y + (plate_size - icon_size) // 2
            picture = slide.shapes.add_picture(io.BytesIO(data), icon_x, icon_y, icon_size, icon_size)
            pictures.append(picture)
    return pictures


def _icon_average_luminance(png_bytes: bytes) -> float | None:
    """Средняя относительная яркость (WCAG) непрозрачных пикселей PNG —
    `None`, если файл не декодируется или полностью прозрачен (тогда
    вызывающий код берёт запасной цвет подложки от фона слайда). Пиксели
    с alpha < 16/255 не учитываются — это фон изображения, а не сам
    рисунок иконки, и не должны тянуть среднюю яркость к прозрачности,
    у которой цвета в RGB-каналах вообще не определены содержательно."""
    try:
        # `Image.getdata()` уходит в Pillow 14 (DeprecationWarning) — тот же
        # приём, что уже применил `template/layouts.py`: `tobytes()` на RGBA
        # даёт построчный набор пикселей по 4 байта, без промежуточного
        # объекта, который `getdata()` оборачивал.
        with Image.open(io.BytesIO(png_bytes)) as opened:
            data = opened.convert("RGBA").tobytes()
    except Exception:
        return None
    total = 0.0
    count = 0
    for i in range(0, len(data), 4):
        r, g, b, a = data[i], data[i + 1], data[i + 2], data[i + 3]
        if a < 16:
            continue
        total += (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
        count += 1
    if count == 0:
        return None
    return total / count


# ---------------------------------------------------------------------------
# Геометрия/оформление карточек — общее для всех шести раскладок
# ---------------------------------------------------------------------------


def _card_geometry(profile: TemplateProfile) -> tuple[str, "MSO_AUTO_SHAPE_TYPE", float]:
    """(`prst`, тип автофигуры python-pptx, степень скругления) карточки —
    форма с наибольшим числом вхождений среди `KNOWN_AUTOSHAPE_PRESETS` в
    `profile.shape_vocabulary`. `rect` — честный запасной вариант, когда
    словарь пуст (синтетический профиль в тестах) — любой реальный `.pptx`
    несёт хотя бы прямоугольники."""
    vocab = [e for e in profile.shape_vocabulary if e.prst in KNOWN_AUTOSHAPE_PRESETS]
    if not vocab:
        return "rect", MSO_AUTO_SHAPE_TYPE.RECTANGLE, 0.0
    best = max(vocab, key=lambda e: e.count)
    return best.prst, MSO_AUTO_SHAPE_TYPE.from_xml(best.prst), best.avg_adj


def _marker_geometry(profile: TemplateProfile) -> tuple[str, "MSO_AUTO_SHAPE_TYPE", float]:
    """То же, что `_card_geometry`, но с предпочтением `ellipse` (маркеры
    цикла/таймлайна традиционно круглые) — используется, ТОЛЬКО когда
    `ellipse` реально есть в словаре шаблона; иначе — та же форма, что и у
    карточек (не изобретаем овал там, где в шаблоне его нет)."""
    vocab = {e.prst: e for e in profile.shape_vocabulary if e.prst in KNOWN_AUTOSHAPE_PRESETS}
    if "ellipse" in vocab:
        entry = vocab["ellipse"]
        return entry.prst, MSO_AUTO_SHAPE_TYPE.from_xml(entry.prst), entry.avg_adj
    return _card_geometry(profile)


def _add_card(
    slide, rect_emu: tuple[int, int, int, int], geometry: tuple[str, "MSO_AUTO_SHAPE_TYPE", float],
    text: str, fill_hex: str, profile: TemplateProfile,
):
    prst, mso_type, avg_adj = geometry
    x, y, w, h = rect_emu
    shape = slide.shapes.add_shape(mso_type, x, y, w, h)
    if prst == "roundRect" and avg_adj > 0:
        try:
            shape.adjustments[0] = avg_adj
        except (IndexError, ValueError):
            pass
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill_hex)
    shape.line.fill.background()

    text_hex = best_contrast_text_color(fill_hex, profile.palette_roles)
    tf = shape.text_frame
    tf.word_wrap = True
    tf.text = text
    family = profile.type_scale.families[0] if profile.type_scale.families else "Arial"
    size_pt = _fit_label_size(text, family, profile, w / EMU_PER_INCH, h / EMU_PER_INCH)
    paragraph = tf.paragraphs[0]
    paragraph.alignment = PP_ALIGN.CENTER
    for run in paragraph.runs:
        run.font.size = Pt(size_pt)
        run.font.name = family
        run.font.color.rgb = _rgb(text_hex)
    return shape


def _add_connector(slide, x1: int, y1: int, x2: int, y2: int, color_hex: str, *, arrow: bool = False):
    connector = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x1, y1, x2, y2)
    connector.line.color.rgb = _rgb(color_hex)
    connector.line.width = Pt(1.5)
    if arrow:
        ln = connector.line._get_or_add_ln()
        tail_end = etree.SubElement(ln, qn("a:tailEnd"))
        tail_end.set("type", "triangle")
    return connector


def _fit_label_size(text: str, family: str, profile: TemplateProfile, width_in: float, height_in: float) -> float:
    line_spacing = profile.type_scale.body_line_spacing
    for step in _LABEL_STEPS:
        size_pt = profile.type_scale.steps.get(step)
        if size_pt is None:
            continue
        metrics = measure(text, family, size_pt, max(width_in, 0.1), line_spacing=line_spacing)
        if metrics.height_in <= height_in:
            return size_pt
    return profile.type_scale.steps.get("micro", profile.type_scale.steps.get("caption", 10.0))


def _connector_color(profile: TemplateProfile) -> str:
    return profile.palette_roles.get("border") or profile.palette_roles.get("muted") or profile.palette_roles.get("on_surface", "#808080")


def _card_fill(slide, profile: TemplateProfile) -> str:
    """Заливка карточки — полюс surface/on_surface, КОНТРАСТНЫЙ фактическому
    фону `slide` (Task 10 отчёт, находка №4, см. `colorpick.
    pop_pair_for_luminance`): карточке нужно быть ВИДИМОЙ на фоне слайда,
    не слиться с ним — слепой `palette_roles["surface"]` (или совпадающий
    с фоном по полюсу) на тёмном лейауте WorkSpace дал чёрную карточку на
    чёрном фоне, видимым остался только текст поверх пустоты."""
    fill_hex, _text_hex = pop_pair_for_luminance(slide_background_luminance(slide, profile), profile.palette_roles)
    return fill_hex


def _accent_fill(profile: TemplateProfile) -> str:
    return profile.palette_roles.get("accent") or profile.palette_roles.get("brand", "#3366CC")


def _level_colors(profile: TemplateProfile, n: int) -> list[str]:
    palette = list(profile.chart_series) or [_accent_fill(profile)]
    if len(palette) >= n:
        return palette[:n]
    return [palette[i % len(palette)] for i in range(n)]


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color.lstrip("#").upper())


def _emu_box(box: Box, profile: TemplateProfile) -> tuple[int, int, int, int]:
    return (
        round(box.left * profile.canvas_width_emu),
        round(box.top * profile.canvas_height_emu),
        round(box.width * profile.canvas_width_emu),
        round(box.height * profile.canvas_height_emu),
    )


def _local_rect(box: Box, profile: TemplateProfile, lx: float, ty: float, lw: float, lh: float) -> tuple[int, int, int, int]:
    """Прямоугольник в EMU по координатам, заданным долями ВНУТРИ `box`
    (0..1 каждая) — упрощает геометрию шести раскладок ниже: они считают
    свои позиции относительно единичного квадрата, не абсолютных долей
    холста."""
    left = (box.left + lx * box.width) * profile.canvas_width_emu
    top = (box.top + ty * box.height) * profile.canvas_height_emu
    width = lw * box.width * profile.canvas_width_emu
    height = lh * box.height * profile.canvas_height_emu
    return round(left), round(top), round(max(width, 1)), round(max(height, 1))


# ---------------------------------------------------------------------------
# Шесть раскладок
# ---------------------------------------------------------------------------


def _build_process(slide, box: Box, items: list[str], profile: TemplateProfile) -> list:
    n = len(items)
    geometry = _card_geometry(profile)
    fill = _card_fill(slide, profile)
    conn_color = _connector_color(profile)
    card_w = (1.0 - _GUTTER * (n - 1)) / n
    card_h = 0.6
    top = (1.0 - card_h) / 2

    shapes = []
    centers = []
    for i, label in enumerate(items):
        lx = i * (card_w + _GUTTER)
        rect = _local_rect(box, profile, lx, top, card_w, card_h)
        shapes.append(_add_card(slide, rect, geometry, label, fill, profile))
        cx, cy, cw, ch = rect
        centers.append((cx + cw, cy + ch // 2))  # правый край, по центру высоты

    for i in range(n - 1):
        x1, y1 = centers[i]
        x2 = x1 + round(_GUTTER * box.width * profile.canvas_width_emu)
        shapes.append(_add_connector(slide, x1, y1, x2, y1, conn_color, arrow=True))
    return shapes


def _build_cycle(slide, box: Box, items: list[str], profile: TemplateProfile) -> list:
    n = len(items)
    geometry = _card_geometry(profile)
    colors = _level_colors(profile, n)
    conn_color = _connector_color(profile)
    card_w, card_h = 0.26, 0.22
    cx, cy = 0.5, 0.5
    rx, ry = 0.37, 0.37

    shapes = []
    centers = []
    for i in range(n):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        px = cx + rx * math.cos(angle)
        py = cy + ry * math.sin(angle)
        lx, ty = px - card_w / 2, py - card_h / 2
        rect = _local_rect(box, profile, lx, ty, card_w, card_h)
        shapes.append(_add_card(slide, rect, geometry, items[i], colors[i % len(colors)], profile))
        rx_emu, ry_emu, rw_emu, rh_emu = rect
        centers.append((rx_emu + rw_emu // 2, ry_emu + rh_emu // 2))

    for i in range(n):
        x1, y1 = centers[i]
        x2, y2 = centers[(i + 1) % n]
        sx = round(x1 + (x2 - x1) * 0.2)
        sy = round(y1 + (y2 - y1) * 0.2)
        ex = round(x1 + (x2 - x1) * 0.8)
        ey = round(y1 + (y2 - y1) * 0.8)
        shapes.append(_add_connector(slide, sx, sy, ex, ey, conn_color, arrow=True))
    return shapes


def _build_hierarchy(slide, box: Box, items: list[str], profile: TemplateProfile) -> list:
    parent, *children = items
    geometry = _card_geometry(profile)
    accent = _accent_fill(profile)
    fill = _card_fill(slide, profile)
    conn_color = _connector_color(profile)

    shapes = []
    parent_w, parent_h = 0.4, 0.22
    parent_rect = _local_rect(box, profile, (1 - parent_w) / 2, 0.03, parent_w, parent_h)
    shapes.append(_add_card(slide, parent_rect, geometry, parent, accent, profile))
    px, py, pw, ph = parent_rect
    parent_bottom = (px + pw // 2, py + ph)

    if children:
        n = len(children)
        child_w = (1.0 - _GUTTER * (n - 1)) / n
        child_h = 0.24
        child_top = 0.55
        for i, label in enumerate(children):
            lx = i * (child_w + _GUTTER)
            rect = _local_rect(box, profile, lx, child_top, child_w, child_h)
            shapes.append(_add_card(slide, rect, geometry, label, fill, profile))
            cx, cy, cw, ch = rect
            child_top_point = (cx + cw // 2, cy)
            shapes.append(_add_connector(slide, *parent_bottom, *child_top_point, conn_color))
    return shapes


def _build_funnel(slide, box: Box, items: list[str], profile: TemplateProfile) -> list:
    n = len(items)
    geometry = _card_geometry(profile)
    colors = _level_colors(profile, n)
    level_h = (1.0 - _GUTTER * (n - 1)) / n
    start_w, min_w = 0.9, 0.3

    shapes = []
    for i, label in enumerate(items):
        w = start_w - (start_w - min_w) * (i / (n - 1) if n > 1 else 0)
        top = i * (level_h + _GUTTER)
        rect = _local_rect(box, profile, (1 - w) / 2, top, w, level_h)
        shapes.append(_add_card(slide, rect, geometry, label, colors[i % len(colors)], profile))
    return shapes


def _build_comparison(slide, box: Box, items: list[str], profile: TemplateProfile) -> list:
    left_items = items[: math.ceil(len(items) / 2)]
    right_items = items[math.ceil(len(items) / 2):]
    geometry = _card_geometry(profile)
    colors = _level_colors(profile, 2)
    conn_color = _connector_color(profile)
    column_w = (1.0 - _GUTTER * 3) / 2

    shapes = []
    for col_index, (col_items, fill) in enumerate([(left_items, colors[0]), (right_items, colors[1 % len(colors)])]):
        col_left = _GUTTER + col_index * (column_w + _GUTTER)
        n = len(col_items) or 1
        card_h = (1.0 - _GUTTER * (n - 1)) / n
        for i, label in enumerate(col_items):
            top = i * (card_h + _GUTTER)
            rect = _local_rect(box, profile, col_left, top, column_w, card_h)
            shapes.append(_add_card(slide, rect, geometry, label, fill, profile))

    divider_x1, divider_y1, _w, _h = _local_rect(box, profile, 0.5, 0.02, 0.0, 0.0)
    _dx, divider_y2, _w2, _h2 = _local_rect(box, profile, 0.5, 0.98, 0.0, 0.0)
    shapes.append(_add_connector(slide, divider_x1, divider_y1, divider_x1, divider_y2, conn_color))
    return shapes


def _build_timeline(slide, box: Box, items: list[str], profile: TemplateProfile) -> list:
    n = len(items)
    marker_geometry = _marker_geometry(profile)
    fill = _accent_fill(profile)
    conn_color = _connector_color(profile)
    marker_d = 0.08
    line_y = 0.4
    label_top = line_y + marker_d / 2 + 0.04
    label_h = 1.0 - label_top - 0.02

    line_x1, line_y1, _w, _h = _local_rect(box, profile, 0.05, line_y, 0.0, 0.0)
    line_x2, _y2, _w2, _h2 = _local_rect(box, profile, 0.95, line_y, 0.0, 0.0)

    shapes = [_add_connector(slide, line_x1, line_y1, line_x2, line_y1, conn_color)]

    positions = [0.05 + (0.9 * i / (n - 1) if n > 1 else 0.45) for i in range(n)]
    card_geometry = _card_geometry(profile)
    for i, label in enumerate(items):
        px = positions[i]
        marker_rect = _local_rect(box, profile, px - marker_d / 2, line_y - marker_d / 2, marker_d, marker_d)
        marker = slide.shapes.add_shape(marker_geometry[1], *marker_rect)
        marker.fill.solid()
        marker.fill.fore_color.rgb = _rgb(fill)
        marker.line.fill.background()
        shapes.append(marker)

        label_w = min(0.9 / n, 0.3)
        label_rect = _local_rect(box, profile, px - label_w / 2, label_top, label_w, max(label_h, 0.1))
        shapes.append(_add_card(slide, label_rect, card_geometry, label, _card_fill(slide, profile), profile))
    return shapes


_BUILDERS = {
    "process": _build_process,
    "cycle": _build_cycle,
    "hierarchy": _build_hierarchy,
    "funnel": _build_funnel,
    "comparison": _build_comparison,
    "timeline": _build_timeline,
}
