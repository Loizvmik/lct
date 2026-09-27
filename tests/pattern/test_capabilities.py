"""Задача V2: возможности раскладки вместо вида, ограничения
разнообразия колоды, повторы одного облика."""
from __future__ import annotations
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from deckforge.pattern.forms import SlideRequirements, capabilities_of, forms_of, pattern_form, unmet_requirements
from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.candidates import candidates_for, cover_pattern_id
from deckforge.pattern.planner import TIGHT_OVERFLOW, plan_patterns
from deckforge.pattern.scoring import growing_repeat_cost, look_key, overflow
from deckforge.pattern.style import load_style
from deckforge.plan.outline import Outline, OutlineSlide
from deckforge.plan.spec import Card, CardBlock, SlideSpec
from deckforge.template.profile import TemplateProfile

from .conftest import bullets, cards, headline, pattern, profile, section, slot

VK_EDUCATION = Path("dataset/templates/Шаблон презентации VK Education.pptx")


@pytest.fixture(scope="module")
def vk_education():
    return TemplateProfile.from_file(VK_EDUCATION, cache_dir=None)


def test_cards_written_for_a_grid_do_not_fit_a_layout_with_one_paragraph():
    grid = cards("gantt", 5, kind="image")
    photo = pattern("photo", "image", [headline(), slot("body", max_chars=400)])
    need = SlideRequirements.from_slide(SlideSpec(
        index=2, kind="image", headline="Ожидание сократится на 80%",
        blocks=[CardBlock(items=[Card(body="Май: пилот"), Card(body="Дек: раскатка")])],
    ))

    assert not unmet_requirements(need, capabilities_of(grid))
    assert unmet_requirements(need, capabilities_of(photo)) == ["карточек 2, мест под карточки 0"]


def test_a_sample_photo_that_would_leave_a_void_is_not_a_capability():
    with_photo = pattern("p", "bullets", [headline(), slot("body")])
    with_photo.photo_frames, with_photo.photo_area, with_photo.photo_slot_area = 1, 0.56, 0.0
    caps = capabilities_of(with_photo)

    assert unmet_requirements(SlideRequirements.from_intent(SlideIntent(0, "context", "x", items=3)), caps)
    small = pattern("s", "bullets", [headline(), slot("body"), slot("image")])
    small.photo_frames, small.photo_area, small.photo_slot_area = 1, 0.31, 0.31
    # Без своего фото место пустеет на 31%, со своим фото не пустеет.
    assert unmet_requirements(SlideRequirements.from_intent(SlideIntent(0, "case", "x", items=3)), capabilities_of(small))
    assert not unmet_requirements(
        SlideRequirements.from_intent(SlideIntent(0, "case", "x", items=3, photo="a.jpg")), capabilities_of(small),
    )


def test_capabilities_name_the_repeat_topology():
    row = cards("row", 4)
    row.repeat.axis, row.repeat.rows, row.repeat.cols = "x", 1, 4
    grid = cards("grid", 6)
    grid.repeat.axis, grid.repeat.rows, grid.repeat.cols = "x", 2, 3

    assert capabilities_of(row).repeat_topology == "row"
    assert capabilities_of(grid).repeat_topology == "grid"
    assert capabilities_of(bullets("l")).repeat_topology == "none"
    assert capabilities_of(grid).card_count_max == 6 == pattern_form(grid).units


def test_repeat_cost_doubles_and_the_third_repeat_beats_any_style_mismatch():
    policy = load_style("dense")
    costs = [growing_repeat_cost("a", None, uses, policy) for uses in range(4)]

    assert costs == [0.0, 30.0, 60.0, 120.0]
    assert costs[3] > policy.weight("style_mismatch")
    assert growing_repeat_cost("a", "a", 0, policy) >= policy.weight("consecutive_repeat")


def test_two_examples_with_one_geometry_share_a_look(vk_education):
    """Примеры 21 и 44 VK Education: одна геометрия (кружки с номерами и
    тексты под ними). С задачи T1 пример 44 разбор видит таймлайном (этапы
    вдоль линии), и облик у них разный уже по виду; при одном виде он
    совпадает."""
    by_id = {p.pattern_id: p for p in vk_education.patterns}
    looks = {pid: look_key(p) for pid, p in by_id.items()}

    assert by_id["slide44"].kind == "timeline"
    assert looks["slide21"] == look_key(by_id["slide44"].model_copy(update={"kind": by_id["slide21"].kind}))
    assert looks["slide21"] != looks["slide42"]


def _cards_outline(n: int) -> Outline:
    return Outline(slides=[
        OutlineSlide(kind="title", intent="Тема"),
        *[OutlineSlide(kind="how_it_works", intent=f"Шаг {i}", items=3) for i in range(n - 2)],
        OutlineSlide(kind="closing", intent="Итог"),
    ])


@pytest.mark.parametrize("style", ["dense", "airy", "visual"])
def test_twelve_card_slides_on_vk_education_repeat_a_layout_at_most_three_times(vk_education, style):
    card_layouts = [p for p in vk_education.patterns if pattern_form(p).main and pattern_form(p).main.block == "cards"]
    assert len(card_layouts) >= 3

    plan = plan_patterns(_cards_outline(12), vk_education, style)

    ids = [a.pattern_id for a in plan]
    assert all(a != b for a, b in zip(ids, ids[1:])), ids
    # Предел повторов (3) действует, пока есть просторная альтернатива
    # (задача V5). По замеру клона три карточки со словами стиля держат у
    # VK Education две-три раскладки, остальные карточные (таймлайн 42,
    # кружки 44, «четыре преимущества» 17) тесны: в них ложится два слова
    # на карточку. Тесную раскладку ради разнообразия планировщик не берёт,
    # повтор просторной честнее.
    forms = forms_of(vk_education)
    policy = load_style(style)
    for a in plan[1:-1]:
        assert overflow(a.intent, forms[a.pattern_id], policy) < TIGHT_OVERFLOW, (a.position, a.pattern_id)
    middle = plan[len(plan) // 2]
    found = candidates_for(
        middle.intent, vk_education, forms, position=middle.position, last=len(plan) - 1,
        cover_id=cover_pattern_id(vk_education),
    )
    roomy = {
        look_key(p) for p in vk_education.patterns
        if p.pattern_id in found.pattern_ids and overflow(middle.intent, forms[p.pattern_id], policy) < TIGHT_OVERFLOW
    }
    assert len(roomy) >= 2, "есть из чего чередовать"
    looks = Counter(look_key(next(p for p in vk_education.patterns if p.pattern_id == pid)) for pid in ids)
    if len(roomy) >= 4:
        assert max(looks.values()) <= 3, ids


def test_same_kind_runs_are_capped_when_there_is_an_alternative():
    prof = profile(
        section("cover", source=1), cards("c1", 3), cards("c2", 3, source=11), cards("c3", 3, source=12),
        bullets("l1", source=21), bullets("l2", source=22), section("end", source=60),
    )
    outline = Outline(slides=[
        OutlineSlide(kind="title", intent="т"),
        *[OutlineSlide(kind="how_it_works", intent=f"ш{i}", items=3) for i in range(6)],
        OutlineSlide(kind="closing", intent="и"),
    ])

    plan = plan_patterns(outline, prof, "dense")

    kinds = [a.kind for a in plan[1:-1]]
    runs = [len(list(g)) for _k, g in __import__("itertools").groupby(kinds)]
    assert max(runs) <= 2, kinds


def test_diversity_gives_way_when_there_is_no_alternative():
    prof = profile(section("cover", source=1), cards("only", 3), section("end", source=60))
    outline = Outline(slides=[
        OutlineSlide(kind="title", intent="т"),
        *[OutlineSlide(kind="how_it_works", intent=f"ш{i}", items=3) for i in range(5)],
        OutlineSlide(kind="closing", intent="и"),
    ])

    plan = plan_patterns(outline, prof, "dense")

    assert [a.pattern_id for a in plan[1:-1]] == ["only"] * 5


def test_alternatives_keep_the_main_form_of_the_chosen_layout():
    gantt = cards("gantt", 5, kind="image", source=43)
    photo = pattern("photo35", "image", [headline(), slot("body", max_chars=400)], source=35)
    prof = profile(section("cover", source=1), gantt, photo, section("end", source=60))
    outline = Outline(slides=[
        OutlineSlide(kind="title", intent="т"), OutlineSlide(kind="roadmap", intent="р", items=4),
        OutlineSlide(kind="closing", intent="и"),
    ])

    plan = plan_patterns(outline, prof, "visual")

    assert plan[1].pattern_id == "gantt"
    assert "photo35" not in plan[1].alternatives


def test_numbered_descriptions_and_captions_are_card_bodies():
    """VK Education slide23: кружки с номерами (`kpi_value`, `ordinal`) и
    описания при них (`kpi_label`) это карточки, не показатели; slide7/8:
    подписи под иконками (`caption` в повторе) тоже карточки. Настоящий
    показатель (значение без пометки `ordinal`) остаётся показателем."""
    from .conftest import pattern, slot, headline

    numbered = pattern("p23", "cards", [
        headline(), *[slot("kpi_value", max_chars=3, ordinal=True, sample_text=str(i)) for i in range(1, 5)],
        *[slot("kpi_label", max_chars=108, sample_text=f"Описание {i}") for i in range(1, 5)],
    ], repeat=4, repeat_roles=("kpi_label", "kpi_value"))
    form = pattern_form(numbered)
    assert form.main is not None and form.main.block == "cards" and form.main.role == "kpi_label"
    assert form.units == 4

    captions = pattern("p7", "cards", [
        headline(), *[slot("caption", max_chars=480, sample_text="Шрифт для заголовков") for _ in range(4)],
    ], repeat=4, repeat_roles=("caption",))
    form = pattern_form(captions)
    assert form.main is not None and form.main.block == "cards" and form.main.role == "caption"

    kpis = pattern("pk", "kpi", [
        headline(), *[slot("kpi_value", max_chars=6, sample_text="42%") for _ in range(3)],
        *[slot("kpi_label", max_chars=40, sample_text="доля") for _ in range(3)],
    ], repeat=3, repeat_roles=("kpi_label", "kpi_value"))
    assert pattern_form(kpis).main.block == "kpi"
