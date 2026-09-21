from lxml import etree
from deckforge.ooxml.color import resolve_color, Color

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
