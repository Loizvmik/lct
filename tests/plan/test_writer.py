"""Тесты `plan.writer.write_slides`/`pick_patterns` (Task 13, Step 3 брифа)."""
from __future__ import annotations
import json

from deckforge.plan.outline import Outline, OutlineSlide, SourceDoc
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec, validate_deck_spec
from deckforge.plan.writer import _flag_repeated_headlines, pick_patterns, write_slides
from deckforge.provider.base import LLMProvider


class _QueueLLM(LLMProvider):
    """Отдаёт заранее заготовленные ответы по очереди (FIFO) — нужен, чтобы
    проверить путь "первый ответ невалиден -> код просит исправить -> второй
    ответ валиден", а не только happy path в один вызов."""

    def __init__(self, responses: list[str | Exception]):
        self._responses = list(responses)

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        if not self._responses:
            raise RuntimeError("_QueueLLM: закончились заготовленные ответы")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _valid_slide_json(headline: str, *, with_number: bool = False) -> str:
    payload = {
        "kind": "bullets",
        "headline": headline,
        "blocks": [{"type": "bullets", "items": ["Первый пункт", "Второй пункт"]}],
    }
    if with_number:
        payload["blocks"][0]["items"][0] = "Сквозная медиана — 6,2 часа"
        payload["source_note"] = "Источник: пилот, июнь—август 2026"
    return json.dumps(payload, ensure_ascii=False)


def _outline(n: int = 3) -> Outline:
    slides = [OutlineSlide(kind="title", intent="Тема")]
    for i in range(n - 2):
        slides.append(OutlineSlide(kind="problem", intent=f"Пункт {i}", needs=["число"]))
    slides.append(OutlineSlide(kind="closing", intent="Итог"))
    return Outline(slides=slides, title="Колода", language="ru")


def test_write_slides_without_llm_produces_valid_fallback_deck(PROFILE):
    outline = _outline(4)
    deck = write_slides(outline, [], PROFILE, llm=None)
    assert isinstance(deck, DeckSpec)
    assert len(deck.slides) == len(outline.slides)
    assert validate_deck_spec(deck) == []
    assert all(s.findings for s in deck.slides)  # запасной вариант честно помечен


def test_write_slides_uses_model_response_when_valid(PROFILE):
    outline = _outline(3)
    llm = _QueueLLM([_valid_slide_json("Заголовок один"), _valid_slide_json("Заголовок два"), _valid_slide_json("Заголовок три")])
    sources = [SourceDoc(name="sources.md", text="Сквозная медиана — 6,2 часа")]
    deck = write_slides(outline, sources, PROFILE, llm=llm)
    assert [s.headline for s in deck.slides] == ["Заголовок один", "Заголовок два", "Заголовок три"]
    assert validate_deck_spec(deck) == []
    assert deck.title == "Колода"
    assert deck.language == "ru"


def test_write_slides_repairs_once_then_falls_back_to_valid_answer(PROFILE):
    outline = _outline(3)
    # Первый ответ на второй слайд несёт цифру без source_note (невалиден) —
    # код обязан попросить исправление один раз и принять второй, валидный
    # ответ, а не сразу деградировать до запасного варианта.
    bad = json.dumps({"kind": "bullets", "headline": "Заголовок", "blocks": [
        {"type": "bullets", "items": ["Медиана — 6,2 часа"]},
    ]}, ensure_ascii=False)  # цифра есть, source_note нет — невалидно
    good = _valid_slide_json("Заголовок исправлен", with_number=True)
    llm = _QueueLLM([_valid_slide_json("Первый"), bad, good, _valid_slide_json("Третий")])
    deck = write_slides(outline, [], PROFILE, llm=llm)
    assert deck.slides[1].headline == "Заголовок исправлен"
    assert validate_deck_spec(deck) == []


def test_write_slides_falls_back_when_model_keeps_sending_invalid_json(PROFILE):
    outline = _outline(3)
    # Оба ответа (первичный и после просьбы исправить) — с неизвестным полем:
    # код обязан деградировать до запасного варианта, а не пропустить слайд
    # или упасть.
    bad = json.dumps({"kind": "bullets", "headline": "X", "unknown_field": 1}, ensure_ascii=False)
    llm = _QueueLLM([_valid_slide_json("Первый"), bad, bad, _valid_slide_json("Третий")])
    deck = write_slides(outline, [], PROFILE, llm=llm)
    assert validate_deck_spec(deck) == []
    assert deck.slides[1].findings  # запасной вариант помечен честно


def test_write_slides_falls_back_on_network_error(PROFILE):
    outline = _outline(2)
    llm = _QueueLLM([RuntimeError("сеть недоступна"), RuntimeError("сеть недоступна")])
    deck = write_slides(outline, [], PROFILE, llm=llm)
    assert validate_deck_spec(deck) == []
    assert all(s.findings for s in deck.slides)


# ---------------------------------------------------------------------------
# _flag_repeated_headlines
# ---------------------------------------------------------------------------


def test_flag_repeated_headlines_catches_the_same_number_stated_differently():
    """Task 13, дефект отчёта задачи №3 (важное, ручная проверка ЛЦТ2026):
    в реально собранной колоде слайды 2 и 4 несли один и тот же факт
    другими словами — "98,5% ожидания устранимы: пилот подтвердил
    эффективность, готов план раскатки" и "98,5% времени заявка находится
    в ожидании, а не обрабатывается" — разный падеж ("ожидания"/
    "ожидании"), разный порядок слов, и буквального пересечения ЗНАЧИМЫХ
    слов нет вовсе (доля 0.0 — "ожидания" != "ожидании" посимвольно) — старый
    бэкстоп (только пересечение целых слов) эту пару пропускал. Индексы
    здесь НЕ соседние (0 и 3, между ними два других слайда) — бэкстоп
    обязан сравнивать ВСЮ колоду, не только соседей (бриф задачи).
    Общая цифра "98,5%" — сильный сигнал того же факта (бриф: "числа в
    заголовке — хороший признак, одна и та же цифра в двух заголовках
    почти всегда означает повтор")."""
    slides = [
        SlideSpec(
            index=0, kind="bullets",
            headline="98,5% ожидания устранимы: пилот подтвердил эффективность, готов план раскатки",
        ),
        SlideSpec(index=1, kind="bullets", headline="Команда и бюджет"),
        SlideSpec(index=2, kind="bullets", headline="Обучение сотрудников"),
        SlideSpec(
            index=3, kind="bullets",
            headline="98,5% времени заявка находится в ожидании, а не обрабатывается.",
        ),
    ]
    _flag_repeated_headlines(slides)
    assert any("слайд 3" in f for f in slides[0].findings), slides[0].findings


def test_flag_repeated_headlines_does_not_fire_on_unrelated_headlines_with_incidental_numbers():
    """Общая цифра — сильный, но не единственный сигнал: две головы,
    делящие короткое случайное число (например, год "2026" в обеих) без
    единого общего значимого слова, не обязаны считаться повтором — иначе
    находка станет бесполезным шумом на любой реальной колоде (обе несут
    "2026" в контексте сроков/дат)."""
    slides = [
        SlideSpec(index=0, kind="bullets", headline="Команда — 2 человека на доработку правил закупок"),
        SlideSpec(index=1, kind="bullets", headline="Раскатка завершится в 2026 году"),
    ]
    _flag_repeated_headlines(slides)
    assert slides[0].findings == []


# ---------------------------------------------------------------------------
# pick_patterns
# ---------------------------------------------------------------------------


def _cards_deck() -> DeckSpec:
    return DeckSpec(title="T", language="ru", slides=[
        SlideSpec(index=0, kind="cards", headline="Заголовок", blocks=[BulletBlock(items=["а", "б"])]),
    ])


def test_pick_patterns_without_llm_assigns_by_capacity(PROFILE):
    deck = pick_patterns(_cards_deck(), PROFILE, llm=None)
    assert deck.slides[0].pattern_id is not None
    assert deck.slides[0].pattern_id in {p.pattern_id for p in PROFILE.patterns if p.kind == "cards"}


def test_pick_patterns_uses_model_choice_when_valid(PROFILE):
    candidate_id = next(p.pattern_id for p in PROFILE.patterns if p.kind == "cards")
    llm = _QueueLLM([json.dumps({"pattern_id": candidate_id})])
    deck = pick_patterns(_cards_deck(), PROFILE, llm=llm)
    assert deck.slides[0].pattern_id == candidate_id


def test_pick_patterns_rejects_hallucinated_pattern_id(PROFILE):
    llm = _QueueLLM([json.dumps({"pattern_id": "not-a-real-pattern-id"})])
    deck = pick_patterns(_cards_deck(), PROFILE, llm=llm)
    assert deck.slides[0].pattern_id in {p.pattern_id for p in PROFILE.patterns if p.kind == "cards"}


def test_pick_patterns_keeps_slide_untouched_when_kind_has_no_candidates(PROFILE):
    deck = DeckSpec(title="T", language="ru", slides=[
        SlideSpec(index=0, kind="cards", headline="H", pattern_id=None),
    ])
    # Профиль без единого паттерна вовсе (пустой) — код не должен падать,
    # просто оставляет слайд как есть (pattern_id=None), решать нечем.
    class _EmptyPatterns:
        patterns: list = []

    result = pick_patterns(deck, _EmptyPatterns(), llm=None)
    assert result.slides[0].pattern_id is None
