"""Задача V1: структура обязана отдать слайд под обязательный визуал слоя
данных (`outline.apply_visual_intents`)."""
from __future__ import annotations
import json
from pathlib import Path

from deckforge.plan.outline import MAX_SLIDES, OutlineSlide, SourceDoc, apply_visual_intents, build_outline
from deckforge.provider.base import LLMProvider

PACKS = Path("fixtures/content-packs")


def _sources(pack: str) -> list[SourceDoc]:
    return [SourceDoc(name="sources.md", text=(PACKS / pack / "sources.md").read_text(encoding="utf-8"))]


class _FakeLLM(LLMProvider):
    def __init__(self, response: str):
        self._response = response

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        return self._response


def _chart_slides(slides):
    return [s for s in slides if s.form == "chart"]


def test_numeric_series_in_sources_gets_a_chart_slide_even_without_the_model():
    outline = build_outline("бриф", _sources("edu-platform"), profile=None, llm=None, target_slides=12)
    charts = _chart_slides(outline.slides)
    refs = {s.visual_intent.data_ref for s in charts}
    assert len(charts) == 2 and refs == {"d1", "d2"}
    assert all(s.visual_intent.required and s.visual_intent.payload()["series"] for s in charts)
    assert outline.slides[0].kind == "title" and outline.slides[-1].kind == "closing"
    assert len(outline.slides) <= MAX_SLIDES


def test_model_chart_slide_gets_the_closest_series_and_the_other_is_added():
    slides = [
        {"kind": "title", "intent": "Учебная платформа: итоги 2026"},
        {"kind": "data", "intent": "Охват растёт от квартала к кварталу", "needs": ["регистрации по кварталам"],
         "form": "chart"},
        *[{"kind": "context", "intent": f"Пункт {i}"} for i in range(8)],
        {"kind": "closing", "intent": "Итог"},
    ]
    llm = _FakeLLM(json.dumps({"slides": slides}, ensure_ascii=False))
    outline = build_outline("бриф", _sources("edu-platform"), profile=None, llm=llm, target_slides=11)
    assert outline.slides[1].visual_intent.data_ref == "d1"
    economy = [s for s in _chart_slides(outline.slides) if s.visual_intent.data_ref == "d2"]
    assert len(economy) == 1


def test_data_slide_that_retells_the_series_as_text_is_turned_into_a_chart():
    """Модель спланировала ряд пунктами списка: код переделывает этот
    пункт под график, а не добавляет второй про те же числа."""
    slides = [
        OutlineSlide(kind="title", intent="Итоги"),
        OutlineSlide(kind="data", intent="Регистраций по кварталам стало больше", needs=["охват по кварталам"]),
        OutlineSlide(kind="closing", intent="Итог"),
    ]
    out = apply_visual_intents(slides, _sources("edu-platform"))
    assert out[1].form == "chart" and out[1].visual_intent.data_ref == "d1"
    assert sum(1 for s in out if s.visual_intent is not None and s.visual_intent.data_ref == "d1") == 1


def test_full_deck_converts_a_plain_slide_instead_of_growing_past_the_limit():
    slides = [OutlineSlide(kind="title", intent="Итоги")]
    slides += [OutlineSlide(kind="context", intent=f"Пункт {i}") for i in range(MAX_SLIDES - 2)]
    slides += [OutlineSlide(kind="closing", intent="Итог")]
    out = apply_visual_intents(slides, _sources("edu-platform"))
    assert len(out) == MAX_SLIDES
    assert len(_chart_slides(out)) == 2


def test_sources_without_chartable_series_do_not_force_a_chart():
    outline = build_outline("бриф", _sources("queue-latency"), profile=None, llm=None, target_slides=12)
    assert _chart_slides(outline.slides) == []


def test_table_ordered_by_the_model_gets_the_source_rows():
    slides = [
        OutlineSlide(kind="title", intent="Пилот"),
        OutlineSlide(kind="data", intent="Пилот: до и после", needs=["показатели пилота"], form="table"),
        OutlineSlide(kind="closing", intent="Итог"),
    ]
    out = apply_visual_intents(slides, _sources("queue-latency"))
    vi = out[1].visual_intent
    assert vi.type == "table" and vi.payload()["rows"][0] == ["Показатель", "До", "После", "Изменение"]
