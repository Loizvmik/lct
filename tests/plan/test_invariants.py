"""Смысловой инвариант пункта (задача V2): содержательный пункт без
содержания не становится разделителем, это отказ контракта и ремонт.
Живой прогон 27 сентября 2026 (visual, слайд 8): «Результат
пилота/проверки» вышел заголовком на героической раскладке."""
from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace

import pytest

from deckforge.compose import builder
from deckforge.plan.invariants import invariant_for, invariant_problems
from deckforge.plan.normalize import normalize_deck
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec
from deckforge.template.profile import TemplateProfile


@pytest.fixture(scope="module")
def profile_vk():
    return TemplateProfile.from_file(Path("dataset/templates/Шаблон презентации VK Education.pptx"), cache_dir=None)


def _contract(outline_kind: str, *, divider: bool = False, evidence=("пилот: −80%",), quote: bool = False):
    return SimpleNamespace(
        outline_kind=outline_kind, is_divider=divider, evidence=evidence, required_visual=None,
        slots=(SimpleNamespace(block="quote" if quote else "bullets"),),
    )


def _profile():
    return SimpleNamespace(patterns=[
        SimpleNamespace(kind=k, repeat=None) for k in ("section", "bullets", "cards", "kpi")
    ])


def _empty(index: int, kind: str = "bullets") -> SlideSpec:
    return SlideSpec(index=index, kind=kind, headline="Результат пилота/проверки", pattern_id="slide13")


def test_content_item_without_blocks_is_a_contract_failure():
    invariant = invariant_for(_contract("case"))

    problems = invariant_problems(_empty(7), invariant)

    assert not invariant.may_be_section and "content" in invariant.required_content_types
    assert problems and "разделителем этот слайд быть не может" in problems[0]
    full = SlideSpec(index=7, kind="bullets", headline="Итог", blocks=[BulletBlock(items=["Пилот: −80% ожидания"])])
    assert invariant_problems(full, invariant) == []


def test_normalize_keeps_the_kind_of_an_empty_content_item_and_flags_it():
    deck = DeckSpec(title="t", language="ru", slides=[_empty(0), _empty(1)])
    invariants = [invariant_for(_contract("case")), invariant_for(_contract("divider", divider=True))]

    out = normalize_deck(deck, _profile(), invariants=invariants)

    content, divider = out.slides
    assert content.kind == "bullets" and content.pattern_id == "slide13"
    assert content.semantic_gap and "ремонт" in content.findings[-1]
    assert divider.kind == "section" and divider.semantic_gap is None


def test_title_closing_and_quote_items_may_stay_a_single_headline():
    for contract in (_contract("title"), _contract("closing"), _contract("context", quote=True)):
        assert invariant_for(contract).may_be_section


def test_without_invariants_normalize_behaves_as_before():
    out = normalize_deck(DeckSpec(title="t", language="ru", slides=[_empty(0)]), _profile())

    assert out.slides[0].kind == "section"


class _Repair:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def shorten(self, slide_spec, pattern_id, problems):
        self.calls.append((pattern_id, problems))
        return self.result


def test_builder_repairs_an_empty_content_item_by_its_contract():
    slide = _empty(7)
    slide.semantic_gap = "пункт плана содержательный"
    written = SlideSpec(index=7, kind="bullets", headline="Пилот сократил ожидание на 80%",
                        blocks=[BulletBlock(items=["Медиана 31,5 ч → 6,2 ч", "4 подразделения"])])
    repair = _Repair(written)
    meta: dict = {}

    builder._refill_semantic_gap(slide, repair, meta)

    assert repair.calls == [("slide13", ["пункт плана содержательный"])]
    assert slide.blocks and slide.kind == "bullets" and slide.semantic_gap is None
    assert meta["semantic_repairs"] == "1"


def test_builder_falls_back_to_a_divider_only_loudly():
    slide = _empty(7)
    slide.semantic_gap = "пункт плана содержательный"
    meta: dict = {}

    builder._refill_semantic_gap(slide, None, meta, [SimpleNamespace(kind="section")])

    assert slide.kind == "section" and slide.pattern_id is None
    assert "брак плана" in slide.findings[-1] and meta["semantic_gaps"] == "1"


def test_without_hero_layouts_the_empty_item_keeps_its_layout():
    slide = _empty(7)
    slide.semantic_gap = "пункт плана содержательный"
    meta: dict = {}

    builder._refill_semantic_gap(slide, None, meta, [SimpleNamespace(kind="bullets")])

    assert slide.kind == "bullets" and slide.pattern_id == "slide13"
    assert "одним заголовком" in slide.findings[-1] and meta["semantic_gaps"] == "1"


def test_forced_divider_never_takes_the_closing_or_cover_layout(profile_vk):
    from deckforge.pattern.candidates import cover_pattern_id, is_closing_pattern

    patterns = [builder._pattern_from_model(m) for m in profile_vk.patterns]
    slide = _empty(5)
    slide.semantic_gap = "пункт плана содержательный"

    builder._refill_semantic_gap(slide, None, {}, patterns, profile_vk)

    chosen = next(p for p in profile_vk.patterns if p.pattern_id == slide.pattern_id)
    assert chosen.kind == "section"
    assert slide.pattern_id != cover_pattern_id(profile_vk) and not is_closing_pattern(chosen, profile_vk)
