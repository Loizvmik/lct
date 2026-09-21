from lxml import etree
from deckforge.ooxml.color import resolve_color, Color, UnresolvedColor

SCHEME = {"dk1": "#000000", "lt1": "#FFFFFF", "dk2": "#0077FF", "lt2": "#FFFFFF",
          "accent1": "#0077FF", "accent2": "#00E9FF"}
CLR_MAP = {"bg1": "lt1", "tx1": "dk1", "bg2": "dk2", "tx2": "lt2"}
A = "http://schemas.openxmlformats.org/drawingml/2006/main"

def node(xml: str):
    return etree.fromstring(f'<a:solidFill xmlns:a="{A}">{xml}</a:solidFill>')

def test_srgb():
    assert resolve_color(node('<a:srgbClr val="0077FF"/>'), SCHEME, CLR_MAP) == Color("#0077FF", 1.0)

def test_scheme_slot():
    assert resolve_color(node('<a:schemeClr val="accent1"/>'), SCHEME, CLR_MAP) == Color("#0077FF", 1.0)

def test_scheme_slot_through_clr_map():
    """Google пишет dk1 напрямую, PowerPoint пишет tx1 — резолвить надо оба."""
    assert resolve_color(node('<a:schemeClr val="tx1"/>'), SCHEME, CLR_MAP) == Color("#000000", 1.0)

def test_alpha_is_kept():
    got = resolve_color(node('<a:srgbClr val="0077FF"><a:alpha val="20000"/></a:srgbClr>'), SCHEME, CLR_MAP)
    assert got == Color("#0077FF", 0.2)

def test_lum_mod_darkens():
    got = resolve_color(node('<a:schemeClr val="accent1"><a:lumMod val="50000"/></a:schemeClr>'), SCHEME, CLR_MAP)
    assert got.hex == "#003B80"

def test_tint_lightens_towards_white():
    got = resolve_color(node('<a:srgbClr val="000000"><a:tint val="50000"/></a:srgbClr>'), SCHEME, CLR_MAP)
    assert got.hex == "#808080"

def test_no_fill_returns_none():
    n = etree.fromstring(f'<a:noFill xmlns:a="{A}"/>')
    assert resolve_color(n, SCHEME, CLR_MAP) is None


# --- Композиция модификаторов: одна float-цепочка, округление один раз ---

def test_two_modifiers_in_a_row_preserve_precision():
    """#7F3FBF, lumOff=50% затем lumMod=50%.

    Честная формула в float: L'=(L+0.5)*0.5=L*0.5+0.25 — для L этого цвета
    (~0.498) даёт почти тот же L, и итоговый цвет практически совпадает с
    исходным (#7F3FBF). Старая реализация (промежуточный round() до байта
    после lumOff) получала на этом же входе #0000FE — расхождение 127/255
    по каналу R, что для видимого цвета совсем не "шум округления", а
    трёхкратная ошибка (см. проверено скриптом отдельно от implementation:
    old_apply_hls(0x7F,0x3F,0xBF, l_offset=0.5) -> (254,254,255) round()
    к байту уже здесь стирает информацию о хроме — почти белый; затем
    lumMod=0.5 на этом почти-белом даёт вырожденный/неустойчивый hue при
    обратном round-trip через HLS и итог (0,0,254) вместо ожидаемого
    #7F3FBF).
    """
    xml = (
        '<a:srgbClr val="7F3FBF">'
        '<a:lumOff val="50000"/><a:lumMod val="50000"/>'
        '</a:srgbClr>'
    )
    got = resolve_color(node(xml), SCHEME, CLR_MAP)
    assert got.hex == "#7F3FBF"


def test_modifier_order_changes_result():
    """lumMod затем lumOff даёт другой цвет, чем lumOff затем lumMod (документный
    порядок, не фиксированный приоритет — ECMA-376 требует именно его)."""
    mod_then_off = resolve_color(
        node('<a:srgbClr val="0077FF"><a:lumMod val="50000"/><a:lumOff val="20000"/></a:srgbClr>'),
        SCHEME, CLR_MAP,
    )
    off_then_mod = resolve_color(
        node('<a:srgbClr val="0077FF"><a:lumOff val="20000"/><a:lumMod val="50000"/></a:srgbClr>'),
        SCHEME, CLR_MAP,
    )
    assert mod_then_off.hex == "#006BE6"
    assert off_then_mod.hex == "#0053B2"
    assert mod_then_off.hex != off_then_mod.hex


# --- Существующие числовые тесты из брифа (lum_mod_darkens, tint_lightens)
# остаются выше без изменений и должны продолжать проходить с новой
# реализацией — оба они однократные модификаторы, где старый и новый способ
# счёта совпадают по построению (нет промежуточного round()-а, который стирал
# бы отличие).


# --- Нераспознанный цвет отличим от «заливки нет» ---

def test_unknown_sys_color_without_lastclr_is_unresolved():
    n = etree.fromstring(
        f'<a:sysClr xmlns:a="{A}" val="someFutureSysColorNotInSpec"/>'
    )
    got = resolve_color(n, SCHEME, CLR_MAP)
    assert isinstance(got, UnresolvedColor)
    assert got is not None


def test_unknown_preset_color_is_unresolved():
    n = etree.fromstring(f'<a:prstClr xmlns:a="{A}" val="notARealPrstColorName"/>')
    got = resolve_color(n, SCHEME, CLR_MAP)
    assert isinstance(got, UnresolvedColor)


def test_scheme_slot_missing_is_unresolved():
    """schemeClr на слот, которого нет ни в clr_map, ни в scheme — тоже
    непонятый цвет, а не «нет заливки»: вызывающий код (сбор палитры,
    аудит «цвет не из палитры») иначе молча потеряет и его."""
    n = node('<a:schemeClr val="accent9"/>')
    got = resolve_color(n, SCHEME, CLR_MAP)
    assert isinstance(got, UnresolvedColor)


def test_unresolved_color_is_distinguishable_from_no_fill():
    unresolved = resolve_color(
        etree.fromstring(f'<a:sysClr xmlns:a="{A}" val="totallyUnknown"/>'), SCHEME, CLR_MAP
    )
    no_fill = resolve_color(etree.fromstring(f'<a:noFill xmlns:a="{A}"/>'), SCHEME, CLR_MAP)
    assert no_fill is None
    assert unresolved is not None
    assert not isinstance(no_fill, UnresolvedColor)
    assert isinstance(unresolved, UnresolvedColor)


def test_sys_color_fallback_table_covers_full_ecma376_set():
    from deckforge.ooxml.color import _SYS_COLOR_FALLBACK
    # ECMA-376 §20.1.10.55 (ST_SystemColorVal) — примерно 28 значений;
    # проверяем, что таблица заметно шире исходных 8 и что типичные
    # «непопулярные» имена из спецификации теперь резолвятся без lastClr.
    assert len(_SYS_COLOR_FALLBACK) >= 28
    for name in ("scrollBar", "btnFace", "menuText", "infoBk", "3dLight", "hotLight"):
        assert name in _SYS_COLOR_FALLBACK
        n = etree.fromstring(f'<a:sysClr xmlns:a="{A}" val="{name}"/>')
        got = resolve_color(n, SCHEME, CLR_MAP)
        assert isinstance(got, Color), name
