"""Выбор титульной раскладки (`candidates.cover_pattern_id`)."""
from __future__ import annotations
from types import SimpleNamespace as NS

from deckforge.pattern.candidates import cover_pattern_id


def _hero(pid: str, slide: int, layout: str, kind: str = "image"):
    return NS(
        pattern_id=pid, source_slide_index=[slide], layout_id=layout, kind=kind, repeat=None,
        slots=[NS(role="headline", sample_text="", fixed=False)],
    )


def test_cover_prefers_title_layout_over_earlier_hero():
    """ЛЦТ2026: слайд 2 «Вводные» героический и стоит раньше, но обложка
    шаблона это слайд 7 на лейауте «Титульный слайд»."""
    profile = NS(
        patterns=[_hero("slide2", 2, "slideLayout4", "photo_text"), _hero("slide7", 7, "slideLayout1"),
                  _hero("slide13", 13, "slideLayout5", "section")],
        layouts=[NS(layout_id="slideLayout1", kind="title"), NS(layout_id="slideLayout4", kind="free")],
    )
    assert cover_pattern_id(profile) == "slide7"


def test_cover_falls_back_to_first_hero_without_title_layout():
    profile = NS(
        patterns=[_hero("slide5", 5, "L2"), _hero("slide3", 3, "L3", "section")],
        layouts=[NS(layout_id="L2", kind="content"), NS(layout_id="L3", kind="content")],
    )
    assert cover_pattern_id(profile) == "slide3"
