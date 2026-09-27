"""Задача T2: фрагмент кода источника идёт на слайд дословно. Структура
отдаёт ему пункт формы `code`, контракт везёт фрагмент готовым, писатель
пишет только заголовок и подпись."""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace

from deckforge.ooxml.geometry import Box
from deckforge.pattern.candidates import candidates_for
from deckforge.pattern.forms import CODE_TIER_SLOT, CODE_TIER_TEXT, pattern_form
from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.planner import PatternAssignment
from deckforge.plan.code import ELLIPSIS, MAX_CODE_COLS, apply_code_intents, find_code_blocks, fit_code
from deckforge.plan.contracts import build_contract, contract_problems
from deckforge.plan.outline import MAX_SLIDES, OutlineSlide, SourceDoc, build_outline
from deckforge.plan.spec import (
    CodeBlock, DeckSpec, SlideSpec, Visual, deck_spec_from_debug_dict, deck_spec_to_dict, visual_from_dict,
)
from deckforge.plan.writer import _write_one_slide
from deckforge.provider.base import LLMProvider

SOURCES = [SourceDoc(
    name="sources.md", text=Path("fixtures/content-packs/full-coverage/sources.md").read_text(encoding="utf-8"),
)]
SOURCE_TEXT = "\n\n".join(f"### {s.name}\n{s.text}" for s in SOURCES)
ROUTE = find_code_blocks(SOURCES)[0]


def _slot(role, *, max_chars=140, box=None, **kw):
    base = dict(role=role, max_chars=max_chars, max_words=None, purpose=None, content_hint=None,
                ordinal=False, fixed=False, sample_text=None, box=box or Box(0.05, 0.3, 0.4, 0.2), size_pt=16.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _pattern(pid, slots, kind="bullets"):
    return SimpleNamespace(
        pattern_id=pid, kind=kind, slots=slots, decor=[], score=0.8, source_slide_index=[3], repeat=None,
        capacity=SimpleNamespace(max_items=1, max_chars_per_item=140, max_bullets=5, max_series=4, max_rows=5,
                                 max_cols=3),
        source_density=0.4, layout_id="L1",
    )


TEXT_AREA = _pattern("area", [_slot("headline", max_chars=50), _slot("body", max_chars=600, box=Box(0.05, 0.25, 0.9, 0.6))])
CODE_SLOT = _pattern("code", [
    _slot("headline", max_chars=50), _slot("code", max_chars=900, box=Box(0.05, 0.25, 0.9, 0.6)),
], kind="code")
CARDS_ONLY = _pattern("small", [_slot("headline", max_chars=50), _slot("body", max_chars=80)])
PROFILE = SimpleNamespace(patterns=[TEXT_AREA, CODE_SLOT, CARDS_ONLY], layouts=[])


def _intent(vi=ROUTE):
    return SlideIntent(index=4, outline_kind="how_it_works", intent="Маршрут задаёт правило", items=1, form="code",
                       visual_intent=vi)


def _contract(pattern, vi=ROUTE):
    return build_contract(PatternAssignment(position=4, intent=_intent(vi), pattern_id=pattern.pattern_id,
                                            kind=pattern.kind), PROFILE, "dense")


class _FakeLLM(LLMProvider):
    def __init__(self, answer: dict):
        self._answer = json.dumps(answer, ensure_ascii=False)

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        return self._answer


def test_fenced_block_is_found_with_language_and_context():
    assert len(find_code_blocks(SOURCES)) == 1
    assert ROUTE.language == "python"
    assert ROUTE.code.splitlines()[0] == "def route(request: Request) -> list[Approver]:"
    assert ROUTE.code.splitlines()[-1] == "    return [line_manager(request.author)]"
    assert len(ROUTE.code.splitlines()) == 6
    assert "Правило" in ROUTE.context and ROUTE.required


def test_short_or_unfenced_code_is_not_a_snippet():
    doc = SourceDoc(name="s", text="Текст с `inline` кодом.\n\n```\nx = 1\n```\n")
    assert find_code_blocks([doc]) == []


def test_long_fragment_is_cut_by_lines_and_width_with_ellipsis():
    code = "\n".join(f"line_{i} = {'x' * (100 if i == 2 else 3)}" for i in range(30))
    fitted, truncated = fit_code(code, max_lines=16)
    lines = fitted.splitlines()
    assert truncated and len(lines) == 16 and lines[-1] == ELLIPSIS
    assert lines[0] == "line_0 = xxx"
    assert len(lines[2]) == MAX_CODE_COLS and lines[2].endswith(ELLIPSIS)
    assert fit_code("a = 1\nb = 2") == ("a = 1\nb = 2", False)


def test_outline_without_model_plans_a_code_slide_for_the_snippet():
    outline = build_outline("бриф", SOURCES, None, None)
    coded = [s for s in outline.slides if s.form == "code"]
    assert len(coded) == 1 and coded[0].visual_intent == ROUTE


def test_model_code_form_without_snippet_is_dropped_and_retold_slide_takes_the_snippet():
    slides = [
        OutlineSlide(kind="title", intent="Тема"),
        OutlineSlide(kind="solution", intent="Правило маршрутизации на Python", needs=["пример правила для закупок"]),
        OutlineSlide(kind="closing", intent="Итог"),
    ]
    out = apply_code_intents(slides, SOURCES, max_slides=MAX_SLIDES)
    assert [s.form for s in out] == [None, "code", None]
    assert out[1].visual_intent == ROUTE
    no_code = apply_code_intents(
        [OutlineSlide(kind="how_it_works", intent="Код", form="code")], [SourceDoc(name="s", text="без кода")],
        max_slides=MAX_SLIDES,
    )
    assert no_code[0].form is None


def test_forms_and_candidates_know_where_code_goes():
    assert pattern_form(CODE_SLOT).code_tier == CODE_TIER_SLOT
    assert pattern_form(TEXT_AREA).code_tier == CODE_TIER_TEXT
    assert pattern_form(CARDS_ONLY).code_tier is None
    forms = {p.pattern_id: pattern_form(p) for p in PROFILE.patterns}
    found = candidates_for(_intent(), PROFILE, forms, position=4, last=10)
    assert set(found.pattern_ids) == {"area", "code"}


def test_contract_carries_the_snippet_and_leaves_no_text_on_the_text_area():
    contract = _contract(TEXT_AREA)
    assert contract.required_visual == "code"
    assert contract.slots == ()
    assert contract.visual_data["code"] == ROUTE.code
    assert contract.visual_limits == {"max_lines": 16, "max_cols": MAX_CODE_COLS}
    prompt = contract.to_prompt()["visual"]
    assert prompt["type"] == "code" and prompt["data"]["language"] == "python"


def test_contract_does_not_let_the_code_change():
    contract = _contract(TEXT_AREA)
    same = SlideSpec(index=4, kind="bullets", headline="Маршрут задаёт одно правило",
                     visual=Visual(kind="code", code=CodeBlock(language="python", code=ROUTE.code)))
    assert contract_problems(same, contract) == []
    edited = SlideSpec(index=4, kind="bullets", headline="Маршрут задаёт одно правило",
                       visual=Visual(kind="code", code=CodeBlock(language="python", code=ROUTE.code.replace("5_000_000", "5"))))
    assert any("код" in p for p in contract_problems(edited, contract))


def test_writer_keeps_the_source_code_whatever_the_model_writes():
    contract = _contract(TEXT_AREA)
    llm = _FakeLLM({
        "headline": "Маршрут заявки задаёт правило в шесть строк",
        "blocks": [],
        "visual": {"kind": "code", "caption": "Крупные закупки идут ещё и финансовому директору",
                   "code": {"language": "python", "code": "def route(): pass"}},
        "source_note": "Материалы проекта",
    })
    slide = _write_one_slide(contract, None, "промпт", SOURCE_TEXT, 12, llm)
    assert slide.visual.kind == "code"
    assert slide.visual.code.code == ROUTE.code
    assert slide.visual.caption == "Крупные закупки идут ещё и финансовому директору"
    assert contract_problems(slide, contract) == []


def test_fallback_slide_without_model_shows_the_code_and_notes_the_cut():
    long = type(ROUTE)(data_ref="c9", language="sql", code="\n".join(f"select {i};" for i in range(40)))
    contract = _contract(TEXT_AREA, long)
    slide = _write_one_slide(contract, None, "промпт", SOURCE_TEXT, 12, None)
    assert slide.visual.code.truncated
    assert len(slide.visual.code.code.splitlines()) == 16
    assert "сокращён" in slide.speaker_notes and "из 40" in slide.speaker_notes


def test_code_survives_the_debug_dump_and_model_json():
    deck = DeckSpec(title="t", language="ru", slides=[SlideSpec(
        index=0, kind="code", headline="Правило",
        visual=Visual(kind="code", caption="подпись", code=CodeBlock(language="python", code=ROUTE.code, truncated=True)),
    )])
    back = deck_spec_from_debug_dict(deck_spec_to_dict(deck))
    assert back.slides[0].visual == deck.slides[0].visual
    assert visual_from_dict({"kind": "code", "caption": "c"}, "v").code is None
