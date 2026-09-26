"""Задача V1: кандидаты под график. Родной график примера, затем
картинка-график (образец оформления), затем крупное текстовое место;
образцы и правила шаблона под обычное содержание не идут."""
from __future__ import annotations
from types import SimpleNamespace

from deckforge.ooxml.geometry import Box
from deckforge.pattern.candidates import candidates_for, cover_pattern_id
from deckforge.pattern.forms import forms_of
from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.planner import plan_patterns
from deckforge.pattern.scoring import static_cost
from deckforge.pattern.style import load_style
from deckforge.template.patterns import CHART_TIER_FRAME, CHART_TIER_NATIVE, CHART_TIER_TEXT

from .conftest import cards, headline, pattern, profile, section


def _slot(role, box, *, max_chars=0, chart_frame=False, sample_text=None):
    return SimpleNamespace(
        role=role, box=box, max_chars=max_chars, max_words=None, purpose=None, content_hint=None,
        ordinal=False, fixed=False, sample_text=sample_text, chart_frame=chart_frame,
    )


def _head():
    return _slot("headline", Box(0.05, 0.05, 0.9, 0.1), max_chars=60)


def _native(pid="native", source=21):
    return pattern(pid, "bullets", [
        _head(), _slot("body", Box(0.55, 0.2, 0.4, 0.2), max_chars=200),
        _slot("chart", Box(0.05, 0.2, 0.45, 0.6)),
    ], source=source)


def _frame(pid="frame", source=48, slide_class="visual_prototype"):
    p = pattern(pid, "image", [_head(), _slot("image", Box(0.05, 0.25, 0.9, 0.6), chart_frame=True)], source=source)
    p.slide_class = slide_class
    return p


def _text_area(pid="area", source=30):
    return pattern(pid, "bullets", [_head(), _slot("body", Box(0.05, 0.2, 0.9, 0.65), max_chars=600)], source=source)


def _photo(pid="photo", source=40):
    return pattern(pid, "image", [_head(), _slot("image", Box(0.5, 0.2, 0.45, 0.7))], source=source)


def _chart_intent(position=2):
    return SlideIntent(index=position, outline_kind="data", intent="Охват по кварталам", items=1, form="chart")


def _costs(prof, style="dense"):
    forms = forms_of(prof)
    policy = load_style(style)
    intent = _chart_intent()
    found = candidates_for(intent, prof, forms, position=2, last=5, cover_id=cover_pattern_id(prof))
    return {
        pid: static_cost(intent, next(p for p in prof.patterns if p.pattern_id == pid), forms[pid], policy,
                         position=2, last=5, cover_id=None, closing_ids=frozenset())
        for pid in found.pattern_ids
    }


def test_chart_tiers_come_from_the_slots():
    forms = forms_of(profile(_native(), _frame(), _text_area(), _photo()))
    assert forms["native"].chart_tier == CHART_TIER_NATIVE
    assert forms["frame"].chart_tier == CHART_TIER_FRAME
    assert forms["area"].chart_tier == CHART_TIER_TEXT
    assert forms["photo"].chart_tier is None


def test_native_chart_then_chart_picture_then_text_area():
    prof = profile(section("cover", source=1), _native(), _frame(), _text_area(), _photo())
    costs = _costs(prof)
    assert "photo" not in costs, "фото-рамка не место для графика"
    assert costs["native"] < costs["frame"] < costs["area"]


def test_visual_style_prefers_a_chart_place_harder():
    prof = profile(section("cover", source=1), _native(), _text_area())
    dense, visual = _costs(prof, "dense"), _costs(prof, "visual")
    assert visual["area"] - visual["native"] > dense["area"] - dense["native"]


def test_chart_places_are_not_given_to_slides_without_a_chart():
    """Без нашего графика картинку-образец и данные-образец родного графика
    сборка удаляет, и слайд пустеет: такие раскладки не для текста."""
    prof = profile(
        section("cover", source=1), _native(), _frame(), _text_area(),
        cards("cards3", 3, source=11), cards("cards3b", 3, source=12),
        pattern("thanks", "section", [headline(sample_text="Спасибо за внимание!")], source=60),
    )
    outline = SimpleNamespace(slides=[
        SimpleNamespace(kind="title", intent="Тема", needs=[], items=None, form=None),
        SimpleNamespace(kind="context", intent="Контекст", needs=["а", "б", "в"], items=3, form=None),
        SimpleNamespace(kind="data", intent="Охват", needs=[], items=1, form="chart"),
        SimpleNamespace(kind="case", intent="Кейс", needs=["а", "б", "в"], items=3, form=None),
        SimpleNamespace(kind="closing", intent="Итог", needs=[], items=None, form=None),
    ])
    plan = plan_patterns(outline, prof, "visual")
    assert plan[2].pattern_id in ("native", "frame")
    for i in (1, 3):
        assert plan[i].pattern_id not in ("native", "frame")


def test_style_guides_and_asset_sheets_are_not_content_layouts():
    guide = _text_area("guide", source=51)
    guide.slide_class = "style_guide"
    sheet = cards("sheet", 3, source=52)
    sheet.slide_class = "asset_sheet"
    prof = profile(section("cover", source=1), guide, sheet, cards("cards3", 3, source=11), _text_area("list"))
    forms = forms_of(prof)
    intent = SlideIntent(index=1, outline_kind="context", intent="Контекст", items=3)
    found = candidates_for(intent, prof, forms, position=1, last=4, cover_id="cover")
    assert "guide" not in found.pattern_ids and "sheet" not in found.pattern_ids
    assert found.relaxed == ()
