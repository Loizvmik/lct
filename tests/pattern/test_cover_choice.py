"""Выбор титульной раскладки (`candidates.cover_pattern_id`)."""
from __future__ import annotations
from types import SimpleNamespace as NS

from pathlib import Path

from deckforge.pattern.candidates import cover_pattern_id, is_closing_pattern


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


def test_photo_and_text_layout_is_not_a_cover():
    """Задача T1: разбор перестал выбрасывать фото в край холста, и
    «текст + фото» (вид `image` с местом под фото на полслайда) вставал
    обложкой раньше настоящего разделителя."""
    photo = _hero("slide6", 6, "L5")
    photo.slots.append(NS(role="image", sample_text="", fixed=False))
    profile = NS(
        patterns=[photo, _hero("slide11", 11, "L7", "section"), _hero("slide30", 30, "L7", "bullets")],
        layouts=[NS(layout_id="L5", kind="content"), NS(layout_id="L7", kind="section")],
    )
    assert cover_pattern_id(profile) == "slide11"


def _closing_candidate(pid: str, slide: int, kind: str, text: str):
    return NS(
        pattern_id=pid, source_slide_index=[slide], layout_id="L9", kind=kind, repeat=None,
        slots=[NS(role="headline", sample_text=text, fixed=False)],
    )


def test_closing_by_headline_text_in_the_tail_of_any_kind():
    """«Спасибо за внимание» в конце шаблона финал, какого бы вида ни была
    раскладка; «Вопросы» в начале шаблона не финал."""
    thanks = _closing_candidate("slide29", 29, "two_col", "Спасибо\nза внимание")
    early = _closing_candidate("slide3", 3, "bullets", "Вопросы для обсуждения")
    profile = NS(patterns=[thanks, early, _closing_candidate("slide30", 30, "cards", "Итоги")], layouts=[])
    assert is_closing_pattern(thanks, profile)
    assert not is_closing_pattern(early, profile)


def test_workspace_cover_and_closing_are_found():
    """VK WorkSpace: финал слайд 29 «Спасибо за внимание» (разбор видит
    две колонки подписей спикеров), обложкой он не встаёт."""
    from deckforge.template.profile import TemplateProfile

    profile = TemplateProfile.from_file(
        Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx"), cache_dir=None,
    )
    assert cover_pattern_id(profile) not in (None, "slide29")
    closing = [p.pattern_id for p in profile.patterns if is_closing_pattern(p, profile)]
    assert closing == ["slide29"]
