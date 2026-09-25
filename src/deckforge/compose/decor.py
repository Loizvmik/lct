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

Правка по итогам визуального ревью (отчёт задачи Task 9, находка №4):
мокап телефона на VK Education (слайд 2) переносил РАМКУ мокапа как
`"shape"`-декор, а область "экрана" внутри неё — ещё один `"shape"` с
картиночной заливкой (`fill_kind="picture"`, `p:blipFill` на автофигуре, не
на `p:pic` — поэтому это не тот случай, что уже пропускается выше:
`ref.kind` майнится по ТИПУ ЭЛЕМЕНТА `p:sp`/`p:pic`/`p:graphicFrame`, а не
по виду заливки, см. `template/patterns.py::_to_decor`). `DecorShape` для
такой заливки несёт только СРЕДНИЙ ЦВЕТ картинки (`_picture_fill_average_
color`) — тот же принцип, что и у уже пропускаемого `kind="picture"`: нет
байтов изображения, нечем воспроизвести исходное фото/скриншот. Закрасить
контейнер под фото сплошным средним цветом — воспроизвести ЧИСЛО, а не
дизайн: получается инородная цветная плашка там, где должен быть снимок
экрана (буквально то, что увидел постановщик — "мокап телефона залит
сплошным синим"). Решение то же, что и для `kind="picture"`/
`"graphic_frame"`: НЕ переносить такой декор вовсе, а не подменять его
плоским цветом. Альтернатива (донести исходные байты картинки до нового
слайда) потребовала бы, чтобы `DecorShape` нёс имя media-парта до самого
`compose/`, а мининг (`template/patterns.py`, вне границ этой задачи) уже
осознанно решил не нести дальше среднего цвета (Task 7 повторное ревью,
находка №6, "мелочи", см. докстроку `DecorShape.fill_kind`) — расширять эту
границу здесь означало бы менять контракт мининга и кеш профиля ради
одного декоративного мокапа на одном из четырёх шаблонов, а рамка мокапа
(если она отдельная фигура без картиночной заливки) переносится как обычно
и остаётся видна пустой — это уже лучше, чем сплошная синяя клякса, и не
хуже, чем то, что и так наследуется от лейаута/мастера."""
from __future__ import annotations
import io
from typing import Callable

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
    image_bytes: Callable[[str], bytes | None] | None = None,
) -> None:
    """Добавляет на `slide` (python-pptx `Slide`) автофигуры/линии,
    воспроизводящие декор паттерна — вызывается builder'ом ПОСЛЕ добавления
    слайда на лейаут паттерна и ДО укладки текстового содержания (декор
    ложится под содержание по z-order документного порядка добавления).

    `image_bytes` — как достать байты картинки по имени части пакета
    (`ppt/media/imageN.png`). Без него картиночный декор пропускается, как
    и раньше: прямые вызовы из тестов и старый код продолжают работать."""
    for shape in decor:
        if shape.fill_kind == "picture":
            # Контейнер под фото/скриншот (мокап телефона и т.п.) — см.
            # докстроку модуля, находка №4: закрасить средним цветом хуже,
            # чем не закрасить вовсе. Тот же принцип, что уже применён ниже
            # к kind="picture"/"graphic_frame", только по виду ЗАЛИВКИ, а не
            # по типу элемента.
            continue
        if shape.kind == "connector":
            _add_connector(slide, shape, canvas_width_emu, canvas_height_emu)
        elif shape.kind == "shape":
            if _is_line_like(shape.box):
                _add_connector(slide, shape, canvas_width_emu, canvas_height_emu)
            else:
                _add_plaque(slide, shape, canvas_width_emu, canvas_height_emu)
        elif shape.kind == "picture" and shape.image_part and image_bytes is not None:
            # Фирменная графика шаблона — иконки, орнаменты, цветные
            # композиции. Раньше пропускалась вместе со всем картиночным
            # декором «потому что среднего цвета мало», и собранный слайд
            # выходил белым листом там, где в шаблоне цветная композиция
            # (замер 25 сентября 2026 на VK Education: пять голых макетов
            # на двенадцать слайдов). Байты картинки берутся из САМОГО
            # шаблона, поэтому воспроизводится она точно, а не средним
            # цветом.
            _add_picture(slide, shape, canvas_width_emu, canvas_height_emu, image_bytes)
        # "graphic_frame" — см. докстроку модуля, намеренно пропускается.


def _add_picture(
    slide, shape: DecorShape, canvas_width_emu: int, canvas_height_emu: int,
    image_bytes: Callable[[str], bytes | None],
) -> None:
    data = image_bytes(shape.image_part or "")
    if not data:
        return
    left = round(shape.box.left * canvas_width_emu)
    top = round(shape.box.top * canvas_height_emu)
    width = max(1, round(shape.box.width * canvas_width_emu))
    height = max(1, round(shape.box.height * canvas_height_emu))
    try:
        pic = slide.shapes.add_picture(io.BytesIO(data), Emu(left), Emu(top), Emu(width), Emu(height))
    except Exception:  # noqa: BLE001 — битая картинка шаблона не вправе ронять сборку
        return
    if shape.rotation:
        pic.rotation = shape.rotation


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
