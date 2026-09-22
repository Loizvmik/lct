"""Тесты `plan.outline.build_outline`/`load_content_pack` (Task 13, Step 1
брифа — планирование содержания)."""
from __future__ import annotations
import json
from pathlib import Path

from deckforge.plan.outline import (
    MAX_SLIDES, MIN_SLIDES, OUTLINE_KINDS, Outline, OutlineSlide, SourceDoc,
    build_outline, load_content_pack,
)
from deckforge.provider.base import LLMProvider

CONTENT_PACK = Path("fixtures/content-packs/queue-latency")


class _FakeLLM(LLMProvider):
    """Отвечает заранее заданным JSON вне зависимости от запроса — тот же
    приём, что `tests/template/test_profile.py::_FakeNamer`, только с
    реальным (не `AssertionError`) телом ответа."""

    def __init__(self, response: str | Exception):
        self._response = response

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _valid_outline_json(n: int) -> str:
    slides = [{"kind": "title", "intent": "Тема"}]
    middle_kinds = ["context", "problem", "data", "solution", "case", "risks", "roadmap"]
    while len(slides) < n - 1:
        slides.append({"kind": middle_kinds[len(slides) % len(middle_kinds)], "intent": f"Пункт {len(slides)}"})
    slides.append({"kind": "closing", "intent": "Итог"})
    return json.dumps({"slides": slides}, ensure_ascii=False)


def test_build_outline_without_llm_returns_valid_skeleton():
    outline = build_outline("бриф", [], profile=None, llm=None, target_slides=12)
    assert isinstance(outline, Outline)
    assert MIN_SLIDES <= len(outline.slides) <= MAX_SLIDES
    assert outline.slides[0].kind == "title"
    assert outline.slides[-1].kind == "closing"
    assert all(s.kind in OUTLINE_KINDS for s in outline.slides)


def test_build_outline_clamps_target_slides_into_tz_range():
    outline = build_outline("бриф", [], profile=None, llm=None, target_slides=3)
    assert len(outline.slides) >= MIN_SLIDES
    outline_big = build_outline("бриф", [], profile=None, llm=None, target_slides=100)
    assert len(outline_big.slides) <= MAX_SLIDES


def test_build_outline_uses_model_response_when_valid():
    llm = _FakeLLM(_valid_outline_json(12))
    outline = build_outline(
        "бриф", [SourceDoc(name="s", text="текст")], profile=None, llm=llm, target_slides=12,
        title="Тест", language="ru",
    )
    assert outline.title == "Тест"
    assert outline.language == "ru"
    assert outline.slides[0].kind == "title"
    assert outline.slides[-1].kind == "closing"
    assert MIN_SLIDES <= len(outline.slides) <= MAX_SLIDES


def test_build_outline_drops_hallucinated_kind_not_in_closed_list():
    raw = json.dumps({"slides": [
        {"kind": "title", "intent": "Тема"},
        {"kind": "gallery", "intent": "Несуществующий тип — должен быть отброшен"},
        {"kind": "closing", "intent": "Итог"},
    ]}, ensure_ascii=False)
    outline = build_outline("бриф", [], profile=None, llm=_FakeLLM(raw), target_slides=10)
    assert all(s.kind in OUTLINE_KINDS for s in outline.slides)
    assert "gallery" not in {s.kind for s in outline.slides}


def test_build_outline_falls_back_on_network_failure():
    outline = build_outline("бриф", [], profile=None, llm=_FakeLLM(RuntimeError("сеть недоступна")), target_slides=12)
    assert MIN_SLIDES <= len(outline.slides) <= MAX_SLIDES
    assert outline.slides[0].kind == "title"


def test_build_outline_falls_back_on_malformed_json():
    outline = build_outline("бриф", [], profile=None, llm=_FakeLLM("не json вовсе"), target_slides=12)
    assert MIN_SLIDES <= len(outline.slides) <= MAX_SLIDES


class _RecordingLLM(LLMProvider):
    """Запоминает `messages` последнего вызова — нужен, чтобы проверить, ЧТО
    именно код кладёт в payload модели (Task 18: `available_forms`), не
    только что ответ модели разбирается."""

    def __init__(self, response: str):
        self._response = response
        self.last_messages = None

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        self.last_messages = messages
        return self._response


def test_build_outline_tells_the_model_about_available_template_forms(PROFILE):
    """Task 18, находка №2 брифа: структуру планируют "с оглядкой" на то,
    что шаблон физически умеет показать — `available_forms` в payload
    обязано нести хотя бы те виды, которые реально есть в разобранном
    профиле (`kind`/`count`/вместимость), а не быть пустым списком, когда
    профиль передан."""
    llm = _RecordingLLM(_valid_outline_json(12))
    build_outline("бриф", [], PROFILE, llm, target_slides=12)
    assert llm.last_messages is not None
    payload = json.loads(llm.last_messages[1]["content"])
    forms = payload["available_forms"]
    assert forms, "available_forms пуст, хотя передан разобранный профиль"
    kinds_seen = {f["kind"] for f in forms}
    actual_kinds = {p.kind for p in PROFILE.patterns}
    assert kinds_seen == actual_kinds
    for f in forms:
        assert f["count"] > 0
        assert f["max_items"] >= 0
        assert f["max_chars_per_item"] >= 0


def test_build_outline_without_a_profile_sends_an_empty_available_forms():
    """`profile=None` (синтетика/ручной вызов без шаблона, как в остальных
    тестах этого файла) — та же честная деградация, что описана в
    докстроке `_summarize_available_forms`: пустой список, не падение."""
    llm = _RecordingLLM(_valid_outline_json(12))
    build_outline("бриф", [], None, llm, target_slides=12)
    payload = json.loads(llm.last_messages[1]["content"])
    assert payload["available_forms"] == []


def test_load_content_pack_reads_frontmatter_and_sources():
    brief, sources, meta = load_content_pack(CONTENT_PACK)
    assert "маршрутизации заявок" in brief
    assert meta["title"] == "Сокращение времени согласования заявок"
    assert meta["language"] == "ru"
    assert meta["target_slides"] == 12
    assert len(sources) == 1
    assert "1 240 заявок" in sources[0].text
