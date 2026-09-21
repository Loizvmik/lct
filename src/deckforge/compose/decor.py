"""Перенос декора раскладки (`Pattern.decor`) на собранный слайд.

Плашки, линии, фоновые фигуры — визуальный язык шаблона (бриф: "раскладка
без декора даст слайд в цветах шаблона, но не в его стиле"). `DecorShape`
несёт геометрию и оформление ЗАЛИВКИ (см. его докстроку в
`template/patterns.py`), но НЕ содержимое картинки — для декоративных
фигур вида `"picture"`/`"graphic_frame"` (логотип, декоративное фото,
картинка-фон, встреченные на слайде-примере как декор) нет ни байтов
изображения, ни relationship на media-часть, только бокс и (иногда)
средний цвет. Такой декор физически нечем "воспроизвести" — и не нужно:
это либо логотип/декоративное фото, уже присутствующее в мастере/лейауте
нативно (колода построена ОТ САМОГО ФАЙЛА шаблона, см. builder.py — то,
что нарисовано на мастере, наследуется каждым слайдом автоматически, без
копирования его в `slide.shapes`), либо фон, который уже несёт сам лейаут
(`slide.slide_layout`, background из p:bg) — копировать его поверх ещё раз
значило бы задвоить логотип (тест брифа
`test_template_master_shapes_are_not_duplicated`) и класть растровую
картинку без реальных пикселей.

Переносятся только `"shape"`/`"connector"` — вид декора, полностью
восстановимый из геометрии и цвета заливки: цветные плашки-подложки
карточек, разделительные линии, декоративные автофигуры без текста.
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.util import Emu

from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.template.patterns import DecorShape

# Порог "боковая линия, а не плашка" — доля холста по меньшей стороне
# бокса. Настоящая плашка/карточка почти всегда заметно толще линии
# разметки; 1% ширины/высоты холста — округлая отсечка (не откалибровано
# под три учебных файла), заметно тоньше типичной плашки, но шире
# погрешности округления EMU.
_LINE_THICKNESS_SHARE = 0.01


def apply_decor(
    slide, decor: list[DecorShape], canvas_width_emu: int, canvas_height_emu: int,
) -> None:
    """Добавляет на `slide` (python-pptx `Slide`) автофигуры/линии,
    воспроизводящие декор паттерна — вызывается builder'ом ПОСЛЕ добавления
    слайда на лейаут паттерна и ДО укладки текстового содержания (декор
    ложится под содержание по z-order документного порядка добавления)."""
    for shape in decor:
        if shape.kind == "connector":
            _add_connector(slide, shape, canvas_width_emu, canvas_height_emu)
        elif shape.kind == "shape":
            if _is_line_like(shape.box):
                _add_connector(slide, shape, canvas_width_emu, canvas_height_emu)
            else:
                _add_plaque(slide, shape, canvas_width_emu, canvas_height_emu)
        # "picture"/"graphic_frame" — см. докстроку модуля, намеренно пропускаются.


def _is_line_like(box: Box) -> bool:
    return box.width <= _LINE_THICKNESS_SHARE or box.height <= _LINE_THICKNESS_SHARE


def _emu_box(box: Box, canvas_width_emu: int, canvas_height_emu: int) -> tuple[int, int, int, int]:
    left = round(box.left * canvas_width_emu)
    top = round(box.top * canvas_height_emu)
    width = max(1, round(box.width * canvas_width_emu))
    height = max(1, round(box.height * canvas_height_emu))
    return left, top, width, height


def _add_plaque(slide, shape: DecorShape, canvas_width_emu: int, canvas_height_emu: int) -> None:
    left, top, width, height = _emu_box(shape.box, canvas_width_emu, canvas_height_emu)
    auto_shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(left), Emu(top), Emu(width), Emu(height))
    auto_shape.line.fill.background()
    _apply_fill(auto_shape, shape)
    _apply_rotation_and_flip(auto_shape, shape)


def _add_connector(slide, shape: DecorShape, canvas_width_emu: int, canvas_height_emu: int) -> None:
    left, top, width, height = _emu_box(shape.box, canvas_width_emu, canvas_height_emu)
    connector = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Emu(left), Emu(top), Emu(left + width), Emu(top + height),
    )
    if shape.has_fill and shape.fill_hex:
        connector.line.color.rgb = RGBColor.from_string(shape.fill_hex.lstrip("#"))
    else:
        connector.line.fill.background()


def _apply_fill(auto_shape, shape: DecorShape) -> None:
    if shape.has_fill and shape.fill_hex:
        auto_shape.fill.solid()
        auto_shape.fill.fore_color.rgb = RGBColor.from_string(shape.fill_hex.lstrip("#"))
    else:
        auto_shape.fill.background()


def _apply_rotation_and_flip(auto_shape, shape: DecorShape) -> None:
    auto_shape.rotation = shape.rotation
    if not shape.flip_h and not shape.flip_v:
        return
    xfrm = auto_shape._element.spPr.find(qn("a:xfrm"))
    if xfrm is None:
        return
    if shape.flip_h:
        xfrm.set("flipH", "1")
    if shape.flip_v:
        xfrm.set("flipV", "1")
