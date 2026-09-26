"""Двумерная сетка повтора (задача V2): таймлайн VK Education `slide42`,
2 ряда × 4 места (в нижнем три). Живой прогон 27 сентября 2026 (airy,
слайд 9): три текста на слайде, семь точек, тексты не под своими точками.
Единица сетки теперь хранит id своих фигур, и обрезка идёт по единице."""
from __future__ import annotations
from pathlib import Path

import pytest
from pptx import Presentation

from deckforge.audit.config import AuditConfig
from deckforge.compose import builder
from deckforge.compose.blocks import expand_repeat, is_grid
from deckforge.compose.clone import sample_slides_by_number, shape_text
from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.walk import walk_shapes
from deckforge.plan.spec import Card, CardBlock, SlideSpec

TEMPLATE = Path("dataset/templates/Шаблон презентации VK Education.pptx")
# Точка таймлайна: маленький круг, меньше 3% холста по обеим сторонам.
_DOT = 0.03


@pytest.fixture(scope="module")
def profile(profile_fixture):
    return profile_fixture(TEMPLATE.name)


@pytest.fixture
def deck():
    prs = Presentation(str(TEMPLATE))
    sources = sample_slides_by_number(prs)
    builder._clear_sample_slides(prs)
    return prs, sources


def _pattern(profile, pattern_id: str):
    return next(builder._pattern_from_model(m) for m in profile.patterns if m.pattern_id == pattern_id)


def _spec(n: int) -> SlideSpec:
    return SlideSpec(
        index=8, kind="cards", headline="Раскатка решения до декабря",
        blocks=[CardBlock(items=[Card(body=f"Этап {i + 1}: шаг раскатки") for i in range(n)])],
        pattern_id="slide42",
    )


def _dots_and_texts(slide, canvas):
    dots, texts = [], []
    for ref in walk_shapes(slide._element, canvas):
        if ref.box is None:
            continue
        if ref.kind == "shape" and ref.box.width < _DOT and ref.box.height < _DOT * 1.5 and not shape_text(ref.element).strip():
            dots.append(ref.box)
        elif "шаг раскатки" in shape_text(ref.element):
            texts.append(ref.box)
    return dots, texts


def test_timeline_is_mined_as_a_grid_of_seven_units(profile):
    pattern = _pattern(profile, "slide42")

    assert is_grid(pattern)
    assert pattern.repeat.rows == 2 and pattern.repeat.cols == 4 and len(pattern.repeat.units) == 7
    assert [(u.row, u.col) for u in pattern.repeat.units][:5] == [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0)]
    # У каждой единицы своя точка.
    assert all(len(u.decor_shape_ids) == 1 for u in pattern.repeat.units)


def test_three_texts_keep_exactly_three_dots_each_above_its_text(profile, deck):
    prs, sources = deck
    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)

    outcome = builder.place_slide_by_clone(prs, _spec(3), _pattern(profile, "slide42"), profile, AuditConfig.load(), sources[42])

    assert outcome.reason is None
    dots, texts = _dots_and_texts(prs.slides[-1], canvas)
    assert len(texts) == 3 and len(dots) == 3, (dots, texts)
    for text in texts:
        assert any(abs(d.left - text.left) < 0.03 and d.top < text.top for d in dots), (text, dots)


def test_scratch_build_draws_only_the_filled_units(profile, deck):
    prs, _sources = deck
    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)

    builder.place_slide(prs, _spec(3), _pattern(profile, "slide42"), profile, AuditConfig.load())

    dots, texts = _dots_and_texts(prs.slides[-1], canvas)
    assert len(texts) == 3 and len(dots) == 3, (dots, texts)


def test_five_cards_wrap_into_the_second_row(profile):
    pattern = _pattern(profile, "slide42")

    units = expand_repeat(pattern, 5, None)

    tops = sorted({round(min(s.box.top for s in unit), 2) for unit in units})
    assert len(units) == 5 and len(tops) == 2
