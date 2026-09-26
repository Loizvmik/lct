"""Задача V1: контракт несёт данные визуала, писатель пишет только подписи
и вывод, запасной слайд строит график без модели."""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace

from deckforge.ooxml.geometry import Box
from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.planner import PatternAssignment
from deckforge.plan.contracts import build_contract
from deckforge.plan.data_types import type_sources, visual_intent_for
from deckforge.plan.outline import SourceDoc
from deckforge.plan.writer import _write_one_slide, chart_problems
from deckforge.plan.spec import ChartSeriesData, ChartVisual, SlideSpec, Visual
from deckforge.provider.base import LLMProvider

SOURCES = [SourceDoc(
    name="sources.md", text=Path("fixtures/content-packs/edu-platform/sources.md").read_text(encoding="utf-8"),
)]
SOURCE_TEXT = "\n\n".join(f"### {s.name}\n{s.text}" for s in SOURCES)
COVERAGE = visual_intent_for(next(d for d in type_sources(SOURCES) if d.table.heading == "Охват"))


def _slot(role, *, max_chars=140, box=None, **kw):
    base = dict(role=role, max_chars=max_chars, max_words=None, purpose=None, content_hint=None,
                ordinal=False, fixed=False, sample_text=None, box=box or Box(0.05, 0.3, 0.4, 0.2))
    base.update(kw)
    return SimpleNamespace(**base)


def _pattern(pid, slots):
    return SimpleNamespace(
        pattern_id=pid, kind="bullets", slots=slots, decor=[], score=0.8, source_slide_index=[3], repeat=None,
        capacity=SimpleNamespace(max_items=1, max_chars_per_item=140, max_bullets=5, max_series=4, max_rows=5,
                                 max_cols=3),
        source_density=0.4,
    )


NATIVE = _pattern("native", [
    _slot("headline", max_chars=50), _slot("body", max_chars=300), _slot("chart", max_chars=0, box=Box(0.05, 0.2, 0.45, 0.6)),
])
TEXT_AREA = _pattern("area", [_slot("headline", max_chars=50), _slot("body", max_chars=600, box=Box(0.05, 0.25, 0.9, 0.6))])
PROFILE = SimpleNamespace(patterns=[NATIVE, TEXT_AREA])


def _contract(pattern):
    intent = SlideIntent(index=2, outline_kind="data", intent="Охват растёт", items=1, form="chart",
                         visual_intent=COVERAGE)
    return build_contract(PatternAssignment(position=2, intent=intent, pattern_id=pattern.pattern_id,
                                            kind=pattern.kind), PROFILE, "visual")


class _FakeLLM(LLMProvider):
    def __init__(self, answer: dict):
        self._answer = json.dumps(answer, ensure_ascii=False)
        self.calls = 0

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        self.calls += 1
        return self._answer


def test_contract_hands_the_writer_the_series_and_the_reason():
    prompt = _contract(NATIVE).to_prompt()
    visual = prompt["visual"]
    assert visual["type"] == "chart"
    assert visual["data"]["categories"] == ["I", "II", "III", "IV"]
    assert visual["data"]["series"][0]["values"] == [4100.0, 6300.0, 5800.0, 9400.0]
    assert "динамика" in visual["reason"]


def test_chart_on_the_text_area_leaves_no_text_blocks_to_write():
    """Раскладка без места под график: график встаёт на главное текстовое
    место, и писатель не пишет туда пункты, которые легли бы под него."""
    assert _contract(TEXT_AREA).slots == ()
    assert _contract(NATIVE).slots != ()


def test_fallback_without_a_model_draws_the_chart_from_the_source():
    slide = _write_one_slide(_contract(TEXT_AREA), PROFILE, "", SOURCE_TEXT, 10, None)
    chart = slide.visual.chart
    assert slide.visual.kind == "chart"
    assert chart.categories == ["I", "II", "III", "IV"]
    assert chart.series[2].values == [1190.0, 1940.0, 2030.0, 3480.0]
    assert slide.source_note


def test_writer_numbers_are_replaced_by_the_source_but_its_labels_stay():
    """Модель округлила ряд: код ставит числа источника, подписи осей и
    выделенную точку модели оставляет. Ремонт ради ряда не зовётся."""
    llm = _FakeLLM({
        "headline": "Доведение выросло в четвёртом квартале",
        "blocks": [],
        "visual": {"kind": "chart", "chart": {
            "kind": "line", "categories": ["Q1", "Q2", "Q3", "Q4"],
            "series": [{"name": "Регистрации", "values": [4.1, 6.3, 5.8, 9.4]}],
            "axis_titles": ["Квартал 2026", "Человек"], "highlight_index": 3,
        }},
        "source_note": "Итоги платформы 2026",
    })
    slide = _write_one_slide(_contract(TEXT_AREA), PROFILE, "", SOURCE_TEXT, 10, llm, agent_max_steps=1)
    chart = slide.visual.chart
    assert chart.series[0].values == [4100.0, 6300.0, 5800.0, 9400.0]
    assert chart.categories == ["I", "II", "III", "IV"] and chart.kind == "bar"
    assert chart.axis_titles == ("Квартал 2026", "Человек") and chart.highlight_index == 3
    assert llm.calls == 1


def test_missing_chart_in_the_answer_is_added_with_a_finding():
    llm = _FakeLLM({"headline": "Охват вырос", "blocks": [], "source_note": "Итоги 2026"})
    slide = _write_one_slide(_contract(TEXT_AREA), PROFILE, "", SOURCE_TEXT, 10, llm, agent_max_steps=1)
    assert slide.visual.kind == "chart"
    assert any("в ответе модели его не было" in f for f in slide.findings)


def test_flat_chart_answer_is_lifted_into_visual_chart():
    """Живой прогон V1: модель клала ряд прямо в `visual`, иногда с видом
    графика в `visual.kind`; слайд уходил в запасной вариант."""
    for kind in ("chart", "bar"):
        llm = _FakeLLM({
            "headline": "Охват вырос к IV кварталу",
            "blocks": [],
            "visual": {"kind": kind, "categories": ["I", "II", "III", "IV"],
                       "series": [{"name": "Регистраций", "values": [4100, 6300, 5800, 9400]}],
                       "unit": "человек", "axis_titles": ["Квартал", "Регистрации"]},
            "source_note": "Итоги 2026",
        })
        slide = _write_one_slide(_contract(TEXT_AREA), PROFILE, "", SOURCE_TEXT, 10, llm, agent_max_steps=1)
        assert not any("запасным вариантом" in f for f in slide.findings), slide.findings
        assert slide.visual.chart.axis_titles == ("Квартал", "Регистрации")


def test_numbers_of_a_model_made_chart_are_checked_against_the_sources():
    """Без данных в контракте ряд пишет модель, и её числа сверяются с
    источниками: выдуманное число уходит в ремонт."""
    contract = _contract(NATIVE)
    contract = type(contract)(**{**contract.__dict__, "visual_data": None})
    slide = SlideSpec(index=2, kind="bullets", headline="x", visual=Visual(kind="chart", chart=ChartVisual(
        kind="bar", categories=["I", "II"], series=[ChartSeriesData(name="Регистраций", values=[4100, 4444])],
    )))
    problems = chart_problems(slide, contract, SOURCE_TEXT)
    assert problems and "4444" in problems[0] and "4100" not in problems[0]
