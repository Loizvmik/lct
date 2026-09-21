"""Резолв цвета DrawingML.

python-pptx отдаёт только сырой RGB схемы, а `a:alpha`/`a:lumMod`/`a:lumOff`/
`a:shade`/`a:tint`/`a:satMod` не разбирает и не применяет вовсе. На WorkSpace
этими модификаторами задана значительная часть заливок (`#0077FF` с alpha
20%, `#000000` с alpha 60%) — без них теряется часть визуального языка
шаблона.
"""
from __future__ import annotations
import colorsys
from dataclasses import dataclass

from deckforge.ooxml.ns import local_name

# Слои-обёртки заливки, у которых нет одного разрешимого цвета:
# grpFill наследует цвет группы-родителя (контекст вне этого узла),
# gradFill — градиент, его разбирает usage.py отдельно.
_NO_COLOR_FILL_TAGS = {"noFill", "grpFill", "gradFill"}

# Системные цвета (a:sysClr) без атрибута lastClr — в реальных файлах
# lastClr почти всегда есть (приложение кеширует фактический RGB на момент
# сохранения), это фолбэк на случай его отсутствия. Имена — полный набор
# ST_SystemColorVal из ECMA-376 Part 1, §20.1.10.55 (сопоставлен с Win32
# GetSysColor: sysClr описывает системный цвет ОС, поэтому сама спецификация
# не фиксирует для них конкретный RGB — только имена; значения ниже это
# классическая палитра Windows 2000/XP «Windows Standard», используемая как
# разумное приближение). Если этот путь когда-нибудь окажется горячим на
# реальном шаблоне (lastClr отсутствует) — стоит сверить с фактическим
# файлом, а не доверять слепо: точность этих конкретных байтов не
# гарантирована спецификацией.
_SYS_COLOR_FALLBACK = {
    "scrollBar": "C8C8C8",
    "background": "000000",
    "activeCaption": "0000FF",
    "activeBorder": "D4D0C8",
    "appWorkspace": "808080",
    "window": "FFFFFF",
    "windowFrame": "000000",
    "windowText": "000000",
    "menu": "FFFFFF",
    "menuText": "000000",
    "captionText": "FFFFFF",
    "inactiveBorder": "D4D0C8",
    "inactiveCaption": "808080",
    "inactiveCaptionText": "C0C0C0",
    "highlight": "0000FF",
    "highlightText": "FFFFFF",
    "btnFace": "C0C0C0",
    "btnShadow": "808080",
    "grayText": "808080",
    "btnText": "000000",
    "btnHighlight": "FFFFFF",
    "3dDkShadow": "404040",
    "3dLight": "C0C0C0",
    "infoText": "000000",
    "infoBk": "FFFFE1",
    "hotLight": "0066CC",
    "gradientActiveCaption": "0000FF",
    "gradientInactiveCaption": "808080",
    "menuHighlight": "0000FF",
    "menuBar": "C0C0C0",
}

# Именованные цвета DrawingML (a:prstClr) — значения совпадают с
# палитрой CSS/X11, атрибут val приходит в camelCase, ключи здесь
# нормализованы к нижнему регистру.
_PRESET_COLORS: dict[str, str] = {
    "aliceblue": "F0F8FF", "antiquewhite": "FAEBD7", "aqua": "00FFFF",
    "aquamarine": "7FFFD4", "azure": "F0FFFF", "beige": "F5F5DC",
    "bisque": "FFE4C4", "black": "000000", "blanchedalmond": "FFEBCD",
    "blue": "0000FF", "blueviolet": "8A2BE2", "brown": "A52A2A",
    "burlywood": "DEB887", "cadetblue": "5F9EA0", "chartreuse": "7FFF00",
    "chocolate": "D2691E", "coral": "FF7F50", "cornflowerblue": "6495ED",
    "cornsilk": "FFF8DC", "crimson": "DC143C", "cyan": "00FFFF",
    "darkblue": "00008B", "darkcyan": "008B8B", "darkgoldenrod": "B8860B",
    "darkgray": "A9A9A9", "darkgrey": "A9A9A9", "darkgreen": "006400",
    "darkkhaki": "BDB76B", "darkmagenta": "8B008B", "darkolivegreen": "556B2F",
    "darkorange": "FF8C00", "darkorchid": "9932CC", "darkred": "8B0000",
    "darksalmon": "E9967A", "darkseagreen": "8FBC8F", "darkslateblue": "483D8B",
    "darkslategray": "2F4F4F", "darkslategrey": "2F4F4F", "darkturquoise": "00CED1",
    "darkviolet": "9400D3", "deeppink": "FF1493", "deepskyblue": "00BFFF",
    "dimgray": "696969", "dimgrey": "696969", "dodgerblue": "1E90FF",
    "firebrick": "B22222", "floralwhite": "FFFAF0", "forestgreen": "228B22",
    "fuchsia": "FF00FF", "gainsboro": "DCDCDC", "ghostwhite": "F8F8FF",
    "gold": "FFD700", "goldenrod": "DAA520", "gray": "808080", "grey": "808080",
    "green": "008000", "greenyellow": "ADFF2F", "honeydew": "F0FFF0",
    "hotpink": "FF69B4", "indianred": "CD5C5C", "indigo": "4B0082",
    "ivory": "FFFFF0", "khaki": "F0E68C", "lavender": "E6E6FA",
    "lavenderblush": "FFF0F5", "lawngreen": "7CFC00", "lemonchiffon": "FFFACD",
    "lightblue": "ADD8E6", "lightcoral": "F08080", "lightcyan": "E0FFFF",
    "lightgoldenrodyellow": "FAFAD2", "lightgray": "D3D3D3", "lightgrey": "D3D3D3",
    "lightgreen": "90EE90", "lightpink": "FFB6C1", "lightsalmon": "FFA07A",
    "lightseagreen": "20B2AA", "lightskyblue": "87CEFA", "lightslategray": "778899",
    "lightslategrey": "778899", "lightsteelblue": "B0C4DE", "lightyellow": "FFFFE0",
    "lime": "00FF00", "limegreen": "32CD32", "linen": "FAF0E6",
    "magenta": "FF00FF", "maroon": "800000", "mediumaquamarine": "66CDAA",
    "mediumblue": "0000CD", "mediumorchid": "BA55D3", "mediumpurple": "9370DB",
    "mediumseagreen": "3CB371", "mediumslateblue": "7B68EE", "mediumspringgreen": "00FA9A",
    "mediumturquoise": "48D1CC", "mediumvioletred": "C71585", "midnightblue": "191970",
    "mintcream": "F5FFFA", "mistyrose": "FFE4E1", "moccasin": "FFE4B5",
    "navajowhite": "FFDEAD", "navy": "000080", "oldlace": "FDF5E6",
    "olive": "808000", "olivedrab": "6B8E23", "orange": "FFA500",
    "orangered": "FF4500", "orchid": "DA70D6", "palegoldenrod": "EEE8AA",
    "palegreen": "98FB98", "paleturquoise": "AFEEEE", "palevioletred": "DB7093",
    "papayawhip": "FFEFD5", "peachpuff": "FFDAB9", "peru": "CD853F",
    "pink": "FFC0CB", "plum": "DDA0DD", "powderblue": "B0E0E6",
    "purple": "800080", "red": "FF0000", "rosybrown": "BC8F8F",
    "royalblue": "4169E1", "saddlebrown": "8B4513", "salmon": "FA8072",
    "sandybrown": "F4A460", "seagreen": "2E8B57", "seashell": "FFF5EE",
    "sienna": "A0522D", "silver": "C0C0C0", "skyblue": "87CEEB",
    "slateblue": "6A5ACD", "slategray": "708090", "slategrey": "708090",
    "snow": "FFFAFA", "springgreen": "00FF7F", "steelblue": "4682B4",
    "tan": "D2B48C", "teal": "008080", "thistle": "D8BFD8",
    "tomato": "FF6347", "turquoise": "40E0D0", "violet": "EE82EE",
    "wheat": "F5DEB3", "white": "FFFFFF", "whitesmoke": "F5F5F5",
    "yellow": "FFFF00", "yellowgreen": "9ACD32",
}


@dataclass(frozen=True)
class Color:
    hex: str
    alpha: float = 1.0


@dataclass(frozen=True)
class UnresolvedColor:
    """Цветовой элемент присутствует в XML, но его значение нельзя определить.

    Отличается от `None` (заливки нет вовсе — `noFill`/`grpFill`/`gradFill`,
    либо узла нет совсем): здесь заливка есть, просто этот слой её не понял —
    неизвестное имя `sysClr`/`prstClr` без записи в таблице фолбэка,
    отсутствующий слот `schemeClr` в теме/`clr_map`, или вовсе нераспознанный
    тег цветового элемента. Смешивать это с «заливки нет» опасно вдвойне:
    молча портит и сбор палитры шаблона (пропущенный реальный цвет), и
    аудит «цвет не из палитры» (ложно решит, что заливки не было). Вызывающий
    код должен собирать список `UnresolvedColor` по всему шаблону и показать
    человеку, а не падать на первом и не путать с честным `noFill`.
    """
    tag: str
    val: str | None
    reason: str


def resolve_color(
    node, scheme: dict[str, str], clr_map: dict[str, str],
) -> Color | UnresolvedColor | None:
    """Разрешает цвет заливки узла OOXML (`a:solidFill`/`a:noFill`/... или сам цветовой элемент).

    `noFill`, `grpFill` и `gradFill` возвращают `None` (градиент разбирает
    usage.py) — заливки в документном смысле действительно нет. Цветовой
    элемент, значение которого не удалось разобрать (неизвестный `sysClr`/
    `prstClr`, ненайденный слот `schemeClr`, нераспознанный тег), возвращает
    `UnresolvedColor`, а не `None` — см. его докстроку.
    """
    if node is None:
        return None
    tag = local_name(node)
    if tag in _NO_COLOR_FILL_TAGS:
        return None
    if tag == "solidFill":
        color_el = next(iter(node), None)
        if color_el is None:
            return None
        return _resolve_color_element(color_el, scheme, clr_map)
    # допускаем и прямую передачу самого цветового элемента (без обёртки solidFill)
    return _resolve_color_element(node, scheme, clr_map)


def _resolve_color_element(
    el, scheme: dict[str, str], clr_map: dict[str, str],
) -> Color | UnresolvedColor | None:
    tag = local_name(el)
    val = el.get("val")
    base = _base_hex(tag, val, el, scheme, clr_map)
    if isinstance(base, UnresolvedColor):
        return base
    if base is None:
        return None

    # Вся цепочка модификаторов считается в float (0..1 на канал) один раз,
    # без промежуточных round() и без промежуточных возвратов в 8-битный
    # RGB — иначе каждый модификатор теряет до половины бита на округлении,
    # и при двух модификаторах подряд расхождение с честной формулой
    # доходит до нескольких десятков /255 по каналу (см. тест
    # test_two_modifiers_in_a_row_preserve_precision). Байт получается
    # один раз, на выходе, в _frgb_to_hex.
    r, g, b = _hex_to_frgb(base)
    alpha = 1.0
    # Порядок применения — документный (порядок детей в XML), не
    # фиксированный приоритет: <a:lumMod/><a:lumOff/> и <a:lumOff/><a:lumMod/>
    # дают разный результат (ECMA-376 требует именно документный порядок,
    # см. test_modifier_order_changes_result), поэтому здесь просто `for mod
    # in el`, без сортировки по типу модификатора.
    for mod in el:
        mtag = local_name(mod)
        mval = mod.get("val")
        if mval is None:
            continue
        frac = int(mval) / 100000
        if mtag == "alpha":
            # alpha трогает только альфа-канал, не RGB.
            alpha = frac
        elif mtag == "lumMod":
            r, g, b = _apply_hls(r, g, b, l_scale=frac)
        elif mtag == "lumOff":
            r, g, b = _apply_hls(r, g, b, l_offset=frac)
        elif mtag == "satMod":
            r, g, b = _apply_hls(r, g, b, s_scale=frac)
        elif mtag == "shade":
            # ECMA-376: "10% shade — это 10% исходного цвета, смешанные
            # с 90% чёрного" — линейная интерполяция канала к 0.0, в float,
            # зажатая в [0, 1] после шага (см. lumOff-комментарий выше).
            r, g, b = (min(1.0, max(0.0, c * frac)) for c in (r, g, b))
        elif mtag == "tint":
            # аналогично, но смешение с белым (1.0) вместо чёрного.
            r, g, b = (min(1.0, max(0.0, c * frac + (1.0 - frac))) for c in (r, g, b))

    return Color(_frgb_to_hex(r, g, b), alpha)


def _base_hex(
    tag: str, val: str | None, el, scheme: dict[str, str], clr_map: dict[str, str],
) -> str | UnresolvedColor | None:
    """Базовый (до модификаторов) hex цвета — либо маркер «не распознан».

    Возвращает `str` (`"#RRGGBB"`), когда цвет разрешён; `UnresolvedColor`,
    когда элемент — узнаваемый цветовой тег с значением, которое не удалось
    сопоставить с реальным RGB (см. докстроку `UnresolvedColor`). `None` тут
    не возвращается вовсе: «заливки нет» — это состояние выше, на уровне
    `resolve_color`/`_NO_COLOR_FILL_TAGS`, не этой функции.
    """
    if tag == "srgbClr":
        if not val:
            return UnresolvedColor(tag, val, "srgbClr без атрибута val")
        return f"#{val.upper()}"
    if tag == "schemeClr":
        if val is None:
            return UnresolvedColor(tag, val, "schemeClr без атрибута val")
        # сначала пробуем через clr_map (PowerPoint пишет tx1/bg1/tx2/bg2),
        # если слота там нет — берём val напрямую по схеме (так пишет Google: dk1/lt1/...)
        slot = clr_map.get(val, val)
        hex_ = scheme.get(slot) or scheme.get(val)
        if hex_ is None:
            return UnresolvedColor(tag, val, f"слот {slot!r}/{val!r} не найден ни в теме, ни в clr_map")
        return hex_
    if tag == "sysClr":
        last = el.get("lastClr")
        if last:
            return f"#{last.upper()}"
        fallback = _SYS_COLOR_FALLBACK.get(val or "")
        if fallback is None:
            return UnresolvedColor(
                tag, val, f"sysClr {val!r} без @lastClr и без записи в таблице фолбэка",
            )
        return f"#{fallback}"
    if tag == "prstClr":
        found = _PRESET_COLORS.get((val or "").lower())
        if found is None:
            return UnresolvedColor(tag, val, f"prstClr {val!r} не найден в таблице именованных цветов")
        return f"#{found}"
    return UnresolvedColor(tag, val, f"нераспознанный тег цветового элемента {tag!r}")


def _apply_hls(r: float, g: float, b: float, *, l_scale: float = 1.0, l_offset: float = 0.0,
               s_scale: float = 1.0) -> tuple[float, float, float]:
    """HLS-модификация канала на float RGB (0..1), без округления до байта.

    `colorsys` уже работает в диапазоне 0..1, поэтому здесь нет отдельного
    /255 туда-обратно — только зажатие L и S в [0, 1] после применения
    (иначе `lumOff` может увести светлоту за единицу, а composed-цепочка
    из нескольких модификаторов — накопить недопустимые значения).
    """
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = min(1.0, max(0.0, l * l_scale + l_offset))
    s = min(1.0, max(0.0, s * s_scale))
    return colorsys.hls_to_rgb(h, l, s)


def _hex_to_frgb(hex_: str) -> tuple[float, float, float]:
    """`"#RRGGBB"` → (r, g, b) в диапазоне 0..1 (float, без промежуточного округления)."""
    h = hex_.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


def _frgb_to_hex(r: float, g: float, b: float) -> str:
    """(r, g, b) в 0..1 → `"#RRGGBB"` — единственное место, где канал округляется до байта."""
    r, g, b = (min(255, max(0, round(c * 255))) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"
