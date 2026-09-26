"""Контракт слайда (`plan.contracts`, задача P): что писатель обязан
написать под уже выбранную раскладку, и проверка ответа по нему."""
from __future__ import annotations
from types import SimpleNamespace

from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.planner import PatternAssignment
from deckforge.plan.contracts import build_contract, contract_fill, contract_problems, plan_contracts
from deckforge.plan.outline import Outline, OutlineSlide
from deckforge.plan.spec import BulletBlock, Card, CardBlock, SlideSpec, TableVisual, TextBlock, Visual


def _slot(role, *, max_chars=140, max_words=None, **kw):
    base = dict(role=role, max_chars=max_chars, max_words=max_words, purpose=None, content_hint=None,
                ordinal=False, fixed=False, sample_text=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _pattern(pid, kind, slots, *, repeat=None, roles=("card_title", "card_body")):
    return SimpleNamespace(
        pattern_id=pid, kind=kind, slots=slots, decor=[], score=0.8, source_slide_index=[3],
        repeat=SimpleNamespace(count=repeat, slot_roles=list(roles)) if repeat else None,
        capacity=SimpleNamespace(max_items=repeat or 1, max_chars_per_item=140, max_bullets=5,
                                 max_series=3, max_rows=5, max_cols=3),
        source_density=0.4,
    )


CARDS = _pattern("cards4", "cards", [
    _slot("headline", max_chars=40, max_words=6),
    *[_slot("card_title", max_chars=28, max_words=3) for _ in range(4)],
    *[_slot("card_body", max_chars=100, max_words=16, purpose="описание шага", content_hint="что делаем") for _ in range(4)],
], repeat=4)
LIST = _pattern("list", "bullets", [_slot("headline", max_chars=50), _slot("bullet", max_chars=420, max_words=60)])
PROFILE = SimpleNamespace(patterns=[CARDS, LIST])


def _contract(pattern, items=3, **intent):
    it = SlideIntent(index=1, outline_kind=intent.pop("outline_kind", "how_it_works"), intent="Как это работает",
                     needs=("шаг 1", "шаг 2", "шаг 3"), items=items, **intent)
    return build_contract(PatternAssignment(position=1, intent=it, pattern_id=pattern.pattern_id, kind=pattern.kind),
                          PROFILE, "dense")


def test_cards_contract_names_count_and_limits_from_the_slot_schema():
    contract = _contract(CARDS, items=3)

    assert contract.pattern_id == "cards4" and contract.kind == "cards"
    assert contract.headline.max_words == 6 and contract.headline.target_words == 5
    main = contract.slots[0]
    assert (main.block, main.count) == ("cards", 3)
    assert main.item.max_words == 16 and main.item.target_words == 13
    assert main.title is not None and main.title.max_words == 3 and main.title_slot
    assert main.purpose == "описание шага" and main.content_hint == "что делаем"
    assert contract.evidence == ("шаг 1", "шаг 2", "шаг 3")
    assert contract.target_density == 0.8


def test_a_list_in_one_place_divides_the_place_between_items():
    contract = _contract(LIST, items=4)

    main = contract.slots[0]
    assert (main.block, main.count) == ("bullets", 4)
    assert main.item.max_words == 15 and main.item.max_chars == 104


def test_the_prompt_view_has_no_layout_choice_and_no_coordinates():
    view = _contract(CARDS).to_prompt()

    assert set(view) >= {"headline", "blocks", "intent", "evidence", "slide_kind"}
    text = repr(view)
    assert "box" not in text and "layout" not in text
    assert view["blocks"][0]["count"] == 3 and view["blocks"][0]["body"]["max_words"] == 16


def _cards_slide(n, body="Один два три", title="Шаг"):
    return SlideSpec(index=1, kind="cards", headline="Работа идёт в три шага",
                     blocks=[CardBlock(items=[Card(title=title, body=body) for _ in range(n)])])


def test_a_compliant_answer_has_no_problems_and_fills_every_place():
    contract = _contract(CARDS, items=3)
    slide = _cards_slide(3)

    assert contract_problems(slide, contract) == []
    assert contract_fill(slide, contract) == (4, 4)


def test_wrong_count_long_items_and_extra_blocks_are_named():
    contract = _contract(CARDS, items=3)
    slide = _cards_slide(4, body=" ".join(["слово"] * 20))
    slide.blocks.append(TextBlock(text="лишнее"))
    slide = SlideSpec(index=1, kind="cards", headline=" ".join(["очень"] * 9), blocks=slide.blocks)

    problems = contract_problems(slide, contract)

    assert any("заголовок" in p for p in problems)
    assert any("4 единиц, нужно ровно 3" in p for p in problems)
    assert any("20 слов при пределе 16" in p for p in problems)
    assert any("лишний блок" in p for p in problems)
    ok, total = contract_fill(slide, contract)
    assert ok < total


def test_a_missing_required_block_and_an_unasked_table_are_problems():
    contract = _contract(LIST, items=3)
    slide = SlideSpec(index=1, kind="bullets", headline="Вывод",
                      visual=Visual(kind="table", table=TableVisual(rows=[["a"], ["1"]])))

    problems = contract_problems(slide, contract)

    assert any("нет в ответе" in p for p in problems)
    assert any("visual" in p for p in problems)


def test_a_table_contract_asks_for_the_visual_with_limits():
    table = _pattern("tbl", "table", [_slot("headline"), _slot("table", max_chars=0)])
    profile = SimpleNamespace(patterns=[table])
    it = SlideIntent(index=1, outline_kind="data", intent="Сравнение", items=3, form="table")
    contract = build_contract(PatternAssignment(position=1, intent=it, pattern_id="tbl", kind="table"), profile)

    assert contract.required_visual == "table"
    assert contract.to_prompt()["visual"] == {"type": "table", "max_rows": 5, "max_cols": 3, "max_series": 3}
    good = SlideSpec(index=1, kind="table", headline="Вывод",
                     visual=Visual(kind="table", table=TableVisual(rows=[["a"], ["1"]])))
    assert contract_problems(good, contract) == []


def test_plan_contracts_gives_one_contract_per_planned_slide():
    outline = Outline(slides=[
        OutlineSlide(kind="title", intent="Тема"),
        OutlineSlide(kind="how_it_works", intent="Шаги", needs=["а", "б", "в"]),
        OutlineSlide(kind="closing", intent="Итог"),
    ])
    assignments, contracts = plan_contracts(outline, PROFILE, "dense")

    assert len(assignments) == len(contracts) == 3
    assert [c.pattern_id for c in contracts] == [a.pattern_id for a in assignments]
    assert contracts[1].slots and contracts[1].slots[0].count == 3


def test_bullets_answer_matches_by_type_in_contract_order():
    contract = _contract(LIST, items=2)
    slide = SlideSpec(index=1, kind="bullets", headline="Вывод", blocks=[BulletBlock(items=["а б", "в г"])])
    assert contract_problems(slide, contract) == []
