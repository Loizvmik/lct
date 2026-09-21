"""Тесты разворота повтора под фактическое число элементов — текстовые
слоты (`expand_repeat`, уже покрыт тестами `test_builder.py`) и декор
(`expand_decor`, эта правка). Task 9 повторное ревью, находка №1 ("главная
находка"): `expand_repeat` пересчитывает позиции текстовых слотов под
фактическое число карточек, а декор (плашки-подложки) переносился
`apply_decor` статически, как есть — раскладка, снятая с шести
декоративных рамок, под два элемента содержания оставляла на слайде
четыре пустые рамки. `DecorShape` теперь несёт признак принадлежности
группе повтора (`repeat_group`/`repeat_index`, см. `template/patterns.
py::_decor_repeat_membership`) — `expand_decor` читает его и разворачивает
ТОЛЬКО такой декор, тем же пересчётом шага, что и `expand_repeat`."""
from __future__ import annotations

from deckforge.compose.blocks import expand_decor
from deckforge.ooxml.geometry import Box
from deckforge.template.grid import Grid
from deckforge.template.patterns import Capacity, DecorShape, Pattern, PatternSlot, RepeatSpec


def _grid() -> Grid:
    return Grid(
        margin_left=0.05, margin_right=0.05, margin_top=0.1, margin_bottom=0.1,
        columns=[], gutter=0.02, anchors={}, confidence={}, skipped_no_box=0, native_guides_used=False,
    )


def _plaque(index: int, left: float) -> DecorShape:
    return DecorShape(
        kind="shape", box=Box(left, 0.2, 0.14, 0.5), rotation=0.0, flip_h=False, flip_v=False,
        fill_hex="#FFFFFF", has_fill=True, fill_kind="solid", repeat_group=True, repeat_index=index,
    )


def _six_card_pattern(extra_decor: tuple[DecorShape, ...] = ()) -> Pattern:
    body_slot = PatternSlot(
        role="card_body", box=Box(0.05, 0.25, 0.14, 0.4), size_pt=14.0, color_hex=None,
        align="l", max_chars=200, wraps=True,
    )
    plaques = [_plaque(i, 0.05 + i * 0.15) for i in range(6)]
    return Pattern(
        pattern_id="six-cards", source_slide_index=[0], layout_id="L", kind="cards",
        slots=[body_slot], repeat=RepeatSpec(axis="x", count=6, step=0.15, slot_roles=["card_body"]),
        decor=[*plaques, *extra_decor],
        capacity=Capacity(max_items=6, max_chars_per_item=200, max_bullets=0, max_series=0, max_rows=0, max_cols=0),
        score=1.0, is_dark=False,
    )


def test_decor_group_shrinks_to_the_actual_number_of_items():
    """Раскладка снята с шести декоративных рамок, содержания — два
    элемента: на слайде должны остаться ровно две рамки, не шесть (находка
    ревью: VK Tech, "Риски раскатки" — четыре пустых рамки из шести)."""
    pattern = _six_card_pattern()
    result = expand_decor(pattern, 2, _grid())
    assert len(result) == 2
    assert {d.repeat_index for d in result} == {0, 1}


def test_decor_group_grows_and_recomputes_the_step_when_content_has_more_items():
    """Содержания больше, чем намайненных рамок (6) — рамки размножаются до
    фактического числа, шаг пересчитан так, чтобы все влезли между полями
    шаблона, ничего не выходит за холст."""
    grid = _grid()
    pattern = _six_card_pattern()
    result = expand_decor(pattern, 9, grid)
    assert len(result) == 9
    for d in result:
        assert d.box.left >= grid.margin_left - 1e-9
        assert d.box.left + d.box.width <= 1 - grid.margin_right + 1e-9
    lefts = sorted(d.box.left for d in result)
    steps = [b - a for a, b in zip(lefts, lefts[1:])]
    assert all(abs(s - steps[0]) < 1e-6 for s in steps), "шаг между рамками должен быть равномерным"


def test_decor_outside_the_repeat_group_is_left_untouched():
    """Декор БЕЗ признака группы повтора (логотип, разделительная линия и
    т.п.) переносится как есть, независимо от числа элементов содержания."""
    standalone = DecorShape(
        kind="connector", box=Box(0.0, 0.95, 1.0, 0.001), rotation=0.0, flip_h=False, flip_v=False,
        fill_hex="#000000", has_fill=True, fill_kind="solid", repeat_group=False,
    )
    pattern = _six_card_pattern(extra_decor=(standalone,))
    result = expand_decor(pattern, 2, _grid())
    assert standalone in result
    assert len(result) == 3  # 2 развёрнутые рамки + нетронутая линия


def test_decor_without_a_repeat_consuming_block_is_left_as_mined():
    """`n=None` (на слайде нет `CardBlock`, ничего не разворачивает
    `pattern.repeat`) — декор остаётся как намайнен, менять его не под что."""
    pattern = _six_card_pattern()
    result = expand_decor(pattern, None, _grid())
    assert len(result) == len(pattern.decor)
