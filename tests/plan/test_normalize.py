"""Вырождение содержания под раскладки шаблона (`plan/normalize.py`).

Профиль здесь игрушечный: правилам нужны только виды раскладок и повторы
карточек, а реальный разбор шаблона не добавил бы проверке ничего."""
from __future__ import annotations
from types import SimpleNamespace

from deckforge.plan.normalize import normalize_deck, normalized_count
from deckforge.plan.spec import (
    BulletBlock, Card, CardBlock, DeckSpec, KpiBlock, SlideSpec, TextBlock, validate_deck_spec,
)


def _pattern(kind: str, repeat: int | None = None, roles=("card_title", "card_body")):
    rep = SimpleNamespace(count=repeat, slot_roles=list(roles)) if repeat else None
    return SimpleNamespace(kind=kind, repeat=rep)


def _profile(*patterns):
    return SimpleNamespace(patterns=list(patterns))


FULL = _profile(_pattern("bullets"), _pattern("cards", 3), _pattern("cards", 2), _pattern("kpi", 3))
NO_KPI = _profile(_pattern("bullets"), _pattern("cards", 3))
NO_TWO_CARDS = _profile(_pattern("bullets"), _pattern("cards", 3), _pattern("cards", 4))


def _deck(*slides: SlideSpec) -> DeckSpec:
    return DeckSpec(title="Т", language="ru", slides=list(slides))


def _cards_slide(*cards: Card) -> SlideSpec:
    return SlideSpec(index=2, kind="cards", headline="Вывод", blocks=[CardBlock(items=list(cards))], pattern_id="s7")


def test_single_card_becomes_a_paragraph_with_title_as_first_sentence():
    deck = normalize_deck(_deck(_cards_slide(Card(title="Пилот", body="Четыре подразделения за квартал"))), FULL)

    slide = deck.slides[0]
    assert slide.blocks == [TextBlock(text="Пилот. Четыре подразделения за квартал")]
    assert slide.kind == "bullets"
    assert slide.pattern_id is None, "раскладка писателя подбиралась под карточки"
    assert any("одна карточка" in f for f in slide.findings)
    assert normalized_count(deck) == 1
    assert validate_deck_spec(deck) == []


def test_single_card_with_numeric_title_becomes_a_kpi():
    deck = normalize_deck(_deck(_cards_slide(Card(title="−80%", body="сквозная медиана"))), FULL)

    slide = deck.slides[0]
    assert isinstance(slide.blocks[0], KpiBlock)
    assert slide.blocks[0].items[0].value == "−80%"
    assert slide.blocks[0].items[0].label == "сквозная медиана"
    assert slide.kind == "kpi"


def test_single_card_with_numeric_title_stays_text_without_kpi_layouts():
    deck = normalize_deck(_deck(_cards_slide(Card(title="−80%", body="сквозная медиана"))), NO_KPI)

    assert deck.slides[0].blocks == [TextBlock(text="−80%. сквозная медиана")]


def test_step_number_is_not_a_kpi():
    """Голая цифра в заголовке карточки — номер шага, а не метрика."""
    deck = normalize_deck(_deck(_cards_slide(Card(title="1", body="Собрать заявки"))), FULL)

    assert isinstance(deck.slides[0].blocks[0], TextBlock)


def test_short_numeric_bullets_become_kpis():
    slide = SlideSpec(
        index=3, kind="bullets", headline="Итоги пилота",
        blocks=[BulletBlock(items=["Медиана ожидания — 6,2 ч", "98,5% времени заявка ждёт"])],
        source_note="Пилот, 2026", pattern_id="s3",
    )

    out = normalize_deck(_deck(slide), FULL).slides[0]

    assert out.kind == "kpi"
    assert isinstance(out.blocks[0], KpiBlock)
    assert [(k.value, k.label) for k in out.blocks[0].items] == [
        ("6,2 ч", "Медиана ожидания"), ("98,5%", "Времени заявка ждёт"),
    ]
    assert out.pattern_id is None
    assert any("показател" in f for f in out.findings)


def test_bullets_stay_when_any_item_is_long_or_without_number():
    long_item = SlideSpec(index=1, kind="bullets", headline="Х", blocks=[BulletBlock(items=[
        "Медиана ожидания заявки в очереди составила 6,2 часа по данным пилота", "98,5% ожидание",
    ])], source_note="и")
    no_number = SlideSpec(index=2, kind="bullets", headline="Х", blocks=[BulletBlock(items=["6,2 ч медиана", "без числа"])],
                          source_note="и")
    three = SlideSpec(index=3, kind="bullets", headline="Х", blocks=[BulletBlock(items=["1,5 ч а", "2,5 ч б", "3,5 ч в"])],
                      source_note="и")

    deck = normalize_deck(_deck(long_item, no_number, three), FULL)

    assert all(isinstance(s.blocks[0], BulletBlock) for s in deck.slides)
    assert normalized_count(deck) == 0


def test_numeric_bullets_keep_kind_without_kpi_layouts():
    slide = SlideSpec(index=1, kind="bullets", headline="Х", blocks=[BulletBlock(items=["6,2 ч медиана"])], source_note="и")

    out = normalize_deck(_deck(slide), NO_KPI).slides[0]

    assert out.kind == "bullets"
    assert isinstance(out.blocks[0], BulletBlock)


def test_two_cards_without_two_unit_layout_stay_but_are_recorded():
    slide = _cards_slide(Card(title="А", body="Первое"), Card(title="Б", body="Второе"))

    out = normalize_deck(_deck(slide), NO_TWO_CARDS).slides[0]

    assert out.kind == "cards" and isinstance(out.blocks[0], CardBlock)
    assert out.pattern_id == "s7"
    assert any("две карточки" in f for f in out.findings)


def test_two_cards_with_two_unit_layout_are_left_alone():
    slide = _cards_slide(Card(title="А", body="Первое"), Card(title="Б", body="Второе"))

    out = normalize_deck(_deck(slide), FULL).slides[0]

    assert out.findings == []


def test_a_blockless_content_slide_becomes_a_section():
    """«О чём пойдёт речь» без единого пункта садился на финальную
    раскладку «Спасибо за внимание!» (27 сентября 2026)."""
    from deckforge.plan.normalize import normalize_deck
    from deckforge.plan.spec import DeckSpec, SlideSpec

    deck = DeckSpec(title="t", language="ru", slides=[
        SlideSpec(index=1, kind="bullets", headline="О чём пойдёт речь"),
    ])
    out = normalize_deck(deck, _profile(_pattern("section", roles=("headline",)), _pattern("bullets", roles=("headline", "bullet"))))
    assert out.slides[0].kind == "section"
    assert any("разделитель" in f for f in out.slides[0].findings)

