"""Находки ревью codex 27 сентября 2026 по сборке с нуля на незнакомом
шаблоне: график поверх текста, правая половина под занятым телом, карточки,
которые проверка кандидатов считала потерянными."""
from __future__ import annotations
from types import SimpleNamespace

from deckforge.compose import builder
from deckforge.ooxml.geometry import Box


def _content(role: str, box: Box):
    return SimpleNamespace(slot=SimpleNamespace(box=box, role=role), role_hint=role)


def _grid():
    return SimpleNamespace(margin_left=0.05, margin_right=0.05, margin_top=0.1, margin_bottom=0.05)


def test_text_slot_with_a_paragraph_in_it_is_not_a_chart_frame():
    body = SimpleNamespace(role="body", box=Box(0.05, 0.2, 0.9, 0.6))
    assert builder._slot_is_occupied(body, [Box(0.05, 0.2, 0.9, 0.6)])
    assert not builder._slot_is_occupied(body, [])
    assert not builder._slot_is_occupied(body, [Box(0.05, 0.85, 0.9, 0.1)]), "текст ниже рамки не мешает"
    chart = SimpleNamespace(role="chart", box=Box(0.05, 0.2, 0.9, 0.6))
    assert not builder._slot_is_occupied(chart, [Box(0.05, 0.2, 0.9, 0.6)]), "слот под график всегда его"


def test_right_half_is_not_offered_when_the_body_spans_the_width():
    contents = [
        _content("headline", Box(0.05, 0.1, 0.9, 0.08)),
        _content("body", Box(0.05, 0.22, 0.9, 0.7)),
    ]
    box = builder._free_visual_box(contents, _grid())
    assert box is None or box.right <= 0.5 + 1e-6 or box.top >= 0.92 - 1e-6, box


def test_right_half_is_offered_when_the_text_is_a_narrow_column():
    contents = [
        _content("headline", Box(0.05, 0.1, 0.9, 0.08)),
        _content("body", Box(0.05, 0.22, 0.4, 0.7)),
    ]
    box = builder._free_visual_box(contents, _grid())
    assert box is not None and box.left > 0.5


def test_lost_blocks_matches_the_scratch_render_for_cards_without_a_repeat(monkeypatch):
    calls = []

    def fake_assign(slide_spec, pattern, grid, *, cards_as_text=False):
        calls.append(cards_as_text)
        return [], ([] if cards_as_text else [SimpleNamespace(role="cards")])

    monkeypatch.setattr(builder, "assign_content_with_drops", fake_assign)
    spec = SimpleNamespace(blocks=[object()], visual=None)
    assert builder._lost_blocks(spec, object(), object(), cards_as_text=True) == 0
    assert builder._lost_blocks(spec, object(), object()) == 1
    assert calls == [True, False]
