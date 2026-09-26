"""Глобальный планировщик паттернов (`deckforge.pattern.planner`, задача P):
раскладка на каждый слайд до текста, сразу для всей колоды."""
from __future__ import annotations
import time

import pytest

from deckforge.pattern.intent import SlideIntent, intents_from_outline
from deckforge.pattern.planner import plan_patterns, repick_pattern
from deckforge.plan.outline import Outline, OutlineSlide
from deckforge.plan.spec import SlideSpec, TextBlock

from .conftest import bullets, cards, headline, pattern, profile, section, slot


def _outline(kinds: list[str], *, needs: int = 3) -> Outline:
    return Outline(slides=[
        OutlineSlide(kind=k, intent=f"пункт {i}", needs=[f"факт {j}" for j in range(needs)])
        for i, k in enumerate(kinds)
    ], title="T")


_DECK = ["title", "problem", "how_it_works", "risks", "roadmap", "solution", "case", "closing"]


@pytest.mark.parametrize("style", ["dense", "airy", "visual"])
def test_no_two_neighbours_share_a_layout_when_there_are_alternatives(rich_profile, style):
    plan = plan_patterns(_outline(_DECK), rich_profile, style)

    ids = [a.pattern_id for a in plan]
    assert all(pid is not None for pid in ids)
    assert all(a != b for a, b in zip(ids, ids[1:])), ids


def test_cover_goes_first_and_the_closing_layout_only_last(rich_profile):
    for style in ("dense", "airy", "visual"):
        plan = plan_patterns(_outline(_DECK), rich_profile, style)
        assert plan[0].pattern_id == "cover", (style, plan[0])
        assert plan[-1].pattern_id == "thanks", (style, plan[-1])
        assert "cover" not in [a.pattern_id for a in plan[1:]]
        assert "thanks" not in [a.pattern_id for a in plan[:-1]]


def test_units_decide_the_layout_four_items_never_go_to_a_three_card_grid(rich_profile):
    plan = plan_patterns(_outline(["title", "how_it_works", "closing"], needs=4), rich_profile, "dense")

    assert plan[1].pattern_id != "cards3"


def test_a_single_item_never_gets_a_card_grid(rich_profile):
    plan = plan_patterns(_outline(["title", "problem", "closing"], needs=1), rich_profile, "dense")

    assert not plan[1].pattern_id.startswith("cards")


def test_styles_pull_to_their_preferences(rich_profile):
    kinds = ["title", "how_it_works", "closing"]
    dense = plan_patterns(_outline(kinds, needs=3), rich_profile, "dense")
    assert dense[1].kind == "cards"
    visual = plan_patterns(_outline(kinds, needs=4), rich_profile, "visual")
    # Карточки с декором: визуальный стиль тянется к оформлению.
    assert visual[1].pattern_id == "cards4b"


def test_airy_adds_dividers_that_get_a_hero_layout(rich_profile):
    plan = plan_patterns(_outline(_DECK), rich_profile, "airy")

    dividers = [a for a in plan if a.intent.divider]
    assert dividers and len(plan) <= 15
    assert all(a.kind == "section" and a.pattern_id not in ("cover", "thanks") for a in dividers)
    assert not plan[0].intent.divider
    assert all(not (a.intent.divider and b.intent.divider) for a, b in zip(plan, plan[1:]))


def test_no_dividers_without_a_hero_layout_for_them():
    prof = profile(section("cover", source=1), cards("c3", 3), bullets("l1"), bullets("l2", source=21))
    plan = plan_patterns(_outline(_DECK), prof, "airy")

    assert not any(a.intent.divider for a in plan)


def test_a_photo_needs_a_layout_with_room_for_it(rich_profile):
    with_photo = pattern("photo", "photo_text", [headline(), slot("bullet"), slot("image")], source=30)
    prof = profile(*rich_profile.patterns, with_photo)
    intents = intents_from_outline(_outline(["title", "case", "problem", "closing"]), {1: ("a.jpg", None)})

    plan = plan_patterns(intents, prof, "dense")

    assert plan[1].pattern_id == "photo"
    assert "photo" not in [a.pattern_id for i, a in enumerate(plan) if i != 1]


def test_a_table_needs_a_table_slot_and_nobody_else_takes_it(rich_profile):
    table = pattern("table", "table", [headline(), slot("table")], source=40)
    prof = profile(*rich_profile.patterns, table)
    outline = Outline(slides=[
        OutlineSlide(kind="title", intent="т"), OutlineSlide(kind="data", intent="д", form="table"),
        OutlineSlide(kind="problem", intent="п", needs=["а", "б"]), OutlineSlide(kind="closing", intent="и"),
    ])

    plan = plan_patterns(outline, prof, "dense")

    assert plan[1].pattern_id == "table"
    assert plan[2].pattern_id != "table"


def test_relaxed_constraints_are_named_when_nothing_fits():
    prof = profile(section("cover", source=1), cards("c3", 3), section("s2", source=2))
    outline = _outline(["title", "how_it_works", "closing"], needs=6)

    plan = plan_patterns(outline, prof, "dense")

    assert plan[1].pattern_id == "c3"
    assert "units" in plan[1].relaxed
    assert "раскладка под содержание не найдена" in plan[1].gap_note()


def test_planning_is_deterministic(rich_profile):
    first = plan_patterns(_outline(_DECK), rich_profile, "visual")
    second = plan_patterns(_outline(_DECK), rich_profile, "visual")
    assert [a.pattern_id for a in first] == [a.pattern_id for a in second]


def test_fifteen_slides_and_forty_layouts_plan_in_under_a_second():
    patterns = [section("cover", source=1), section("div", source=2)]
    for i in range(19):
        patterns.append(cards(f"cards{i}", 3 + i % 3, source=10 + i, decor=i % 7))
        patterns.append(bullets(f"list{i}", source=40 + i, words=30 + i * 3))
    prof = profile(*patterns[:40])
    kinds = ["title"] + ["problem", "how_it_works", "risks", "roadmap", "solution", "case", "data"] * 2 \
        + ["closing"]
    outline = _outline(kinds[:15])

    for style in ("dense", "airy", "visual"):
        started = time.monotonic()
        plan = plan_patterns(outline, prof, style)
        assert time.monotonic() - started < 1.0
        assert len({a.pattern_id for a in plan}) >= 10


def test_empty_profile_still_gives_every_slide_a_kind():
    plan = plan_patterns(_outline(["title", "data", "closing"]), profile(), "dense")
    assert [a.pattern_id for a in plan] == [None, None, None]
    assert [a.kind for a in plan] == ["section", "kpi", "section"]


def test_repick_avoids_the_neighbours(rich_profile):
    slide = SlideSpec(index=2, kind="bullets", headline="х", blocks=[TextBlock(text="абзац")])
    ids = ["cover", "list_a", None, "list_c", "thanks"]

    pid = repick_pattern(slide, 2, ids, rich_profile, "dense")

    assert pid not in (None, "list_a", "list_c", "cover", "thanks")


def test_intent_items_come_from_the_outline_first():
    outline = Outline(slides=[
        OutlineSlide(kind="title", intent="т", needs=["x"]),
        OutlineSlide(kind="problem", intent="п", needs=["а", "б"], items=4),
        OutlineSlide(kind="risks", intent="р", needs=["а", "б"]),
        OutlineSlide(kind="agenda", intent="а"),
    ])
    intents = intents_from_outline(outline)
    assert [i.items for i in intents] == [0, 4, 2, 4]
    assert isinstance(intents[0], SlideIntent) and intents[0].is_hero


def test_a_headline_frame_too_short_for_a_conclusion_loses_to_a_normal_one():
    """Рамка заголовка на 9 знаков (подпись у VK Education): писатель
    заведомо нарушит контракт, раскладка берётся, только если другой нет."""
    tiny = bullets("tiny", source=30)
    tiny.slots[0] = headline(max_chars=9)
    prof = profile(section("cover", source=1), tiny, bullets("normal", source=31), section("end", source=90))

    plan = plan_patterns(_outline(["title", "problem", "closing"]), prof, "dense")

    assert plan[1].pattern_id == "normal"
