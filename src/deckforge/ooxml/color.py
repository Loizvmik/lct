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
# lastClr почти всегда есть (приложение кеширует фактический RGB), это
# фолбэк на случай его отсутствия.
_SYS_COLOR_FALLBACK = {
    "windowText": "000000",
    "window": "FFFFFF",
    "highlightText": "FFFFFF",
    "highlight": "0000FF",
    "activeCaption": "0000FF",
    "captionText": "FFFFFF",
    "gradientActiveCaption": "0000FF",
    "gradientInactiveCaption": "808080",
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


def resolve_color(node, scheme: dict[str, str], clr_map: dict[str, str]) -> Color | None:
    """Разрешает цвет заливки узла OOXML (`a:solidFill`/`a:noFill`/... или сам цветовой элемент).

    `noFill`, `grpFill` и `gradFill` возвращают `None` (градиент разбирает usage.py).
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


def _resolve_color_element(el, scheme: dict[str, str], clr_map: dict[str, str]) -> Color | None:
    tag = local_name(el)
    val = el.get("val")
    hex_ = _base_hex(tag, val, el, scheme, clr_map)
    if hex_ is None:
        return None

    r, g, b = _hex_to_rgb(hex_)
    alpha = 1.0
    # модификаторы применяются строго в порядке появления в XML —
    # lumMod и lumOff, например, обычно идут парой и вместе дают
    # L' = L*lumMod + lumOff, что и получается при последовательном
    # применении в документном порядке.
    for mod in el:
        mtag = local_name(mod)
        mval = mod.get("val")
        if mval is None:
            continue
        frac = int(mval) / 100000
        if mtag == "alpha":
            alpha = frac
        elif mtag == "lumMod":
            r, g, b = _apply_hls(r, g, b, l_scale=frac, l_offset=0.0)
        elif mtag == "lumOff":
            r, g, b = _apply_hls(r, g, b, l_scale=1.0, l_offset=frac)
        elif mtag == "satMod":
            r, g, b = _apply_hls(r, g, b, s_scale=frac)
        elif mtag == "shade":
            # ECMA-376: "10% shade — это 10% исходного цвета, смешанные с 90% чёрного"
            r, g, b = (round(c * frac) for c in (r, g, b))
        elif mtag == "tint":
            # аналогично, но смешение с белым вместо чёрного
            r, g, b = (round(c * frac + 255 * (1 - frac)) for c in (r, g, b))

    return Color(_rgb_to_hex(r, g, b), alpha)


def _base_hex(tag: str, val: str | None, el, scheme: dict[str, str], clr_map: dict[str, str]) -> str | None:
    if tag == "srgbClr":
        return f"#{val.upper()}" if val else None
    if tag == "schemeClr":
        if val is None:
            return None
        # сначала пробуем через clr_map (PowerPoint пишет tx1/bg1/tx2/bg2),
        # если слота там нет — берём val напрямую по схеме (так пишет Google: dk1/lt1/...)
        slot = clr_map.get(val, val)
        return scheme.get(slot) or scheme.get(val)
    if tag == "sysClr":
        last = el.get("lastClr")
        if last:
            return f"#{last.upper()}"
        fallback = _SYS_COLOR_FALLBACK.get(val or "")
        return f"#{fallback}" if fallback else None
    if tag == "prstClr":
        found = _PRESET_COLORS.get((val or "").lower())
        return f"#{found}" if found else None
    return None


def _apply_hls(r: int, g: int, b: int, *, l_scale: float = 1.0, l_offset: float = 0.0,
               s_scale: float = 1.0) -> tuple[int, int, int]:
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    l = min(1.0, max(0.0, l * l_scale + l_offset))
    s = min(1.0, max(0.0, s * s_scale))
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return round(r2 * 255), round(g2 * 255), round(b2 * 255)


def _hex_to_rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    r, g, b = (min(255, max(0, c)) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"
