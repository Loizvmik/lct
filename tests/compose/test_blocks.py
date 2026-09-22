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

import pytest

from deckforge.compose.blocks import assign_content, expand_decor, expand_repeat
from deckforge.ooxml.geometry import Box
from deckforge.plan.spec import Kpi, KpiBlock, SlideSpec
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


def test_decor_group_keeps_the_native_step_instead_of_stretching_to_the_full_span():
    """Task 10 отчёт, находка №3: при уменьшении числа элементов шаг НЕ
    растягивается на весь span между полями шаблона (иначе две карточки
    разъезжаются к противоположным краям слайда) — остаётся РОДНОЙ шаг
    раскладки (0.15), группа компактна и выровнена по началу контентной
    области (`grid.margin_left=0.05`), как и первые две намайненные рамки
    исходной шестёрки."""
    pattern = _six_card_pattern()
    result = expand_decor(pattern, 2, _grid())
    lefts = sorted(d.box.left for d in result)
    assert lefts == pytest.approx([0.05, 0.20])


def test_repeat_group_keeps_the_native_step_instead_of_stretching_to_the_full_span():
    """Тот же дефект, что и у декора (см. тест выше), но для ТЕКСТОВЫХ
    слотов `expand_repeat` — обе развёртки обязаны использовать одно и то
    же геометрическое ядро (`_expand_positions`), так текст и его плашка
    не расходятся друг с другом."""
    pattern = _six_card_pattern()
    groups = expand_repeat(pattern, 2, _grid())
    lefts = sorted(g[0].box.left for g in groups)
    assert lefts == pytest.approx([0.05, 0.20])


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


# ---------------------------------------------------------------------------
# Укладка KpiBlock (Task 13, продолжение — обязательная проверка на
# контрольном шаблоне поймала слайд 6: "крупные цифры разбросаны без
# подписей", хотя раскладка несёт парные kpi_value/kpi_label слоты).
# ---------------------------------------------------------------------------


def _kpi_pattern(n_value_slots: int, n_label_slots: int) -> Pattern:
    """`kind="kpi"`-паттерн с `n_value_slots` слотов роли `kpi_value` и
    `n_label_slots` слотов роли `kpi_label` (заведомо МЕНЬШЕ, чем
    `kpi_value`, когда `n_label_slots < n_value_slots` — воспроизводит
    находку обязательной проверки: раскладка несёт больше мест под крупное
    число, чем под подпись к нему), плюс запасной текстовый слот для
    фолбэка непарных метрик."""
    value_slots = [
        PatternSlot(
            role="kpi_value", box=Box(0.05 + i * 0.23, 0.2, 0.2, 0.15), size_pt=32.0, color_hex=None,
            align="ctr", max_chars=10, wraps=False,
        )
        for i in range(n_value_slots)
    ]
    label_slots = [
        PatternSlot(
            role="kpi_label", box=Box(0.05 + i * 0.23, 0.37, 0.2, 0.08), size_pt=12.0, color_hex=None,
            align="ctr", max_chars=30, wraps=True,
        )
        for i in range(n_label_slots)
    ]
    fallback = PatternSlot(
        role="body", box=Box(0.05, 0.5, 0.9, 0.3), size_pt=14.0, color_hex=None,
        align="l", max_chars=400, wraps=True,
    )
    return Pattern(
        pattern_id="kpi-pattern", source_slide_index=[0], layout_id="L", kind="kpi",
        slots=[*value_slots, *label_slots, fallback], repeat=None, decor=[],
        capacity=Capacity(
            max_items=n_value_slots, max_chars_per_item=10, max_bullets=0, max_series=0, max_rows=0, max_cols=0,
        ),
        score=1.0, is_dark=False,
    )


def test_kpi_value_and_label_of_the_same_metric_always_land_in_a_paired_slot_or_neither():
    """Task 13 продолжение, дефект отчёта (слайд 6, контрольный шаблон):
    раскладка несёт 4 слота `kpi_value`, но только 2 слота `kpi_label` —
    раньше `_assign_kpis` клала ЗНАЧЕНИЕ третьей и четвёртой метрики в
    свободный `kpi_value`-слот, а её подпись просто терялась (`if i <
    len(label_slots)`) — на слайде оставались голые числа без подписи.
    Метрика, для которой не хватило ПАРЫ (и значения, и подписи), обязана
    уйти ЦЕЛИКОМ в текстовый фолбэк "значение — подпись", а не разойтись
    по разным слотам/судьбам."""
    pattern = _kpi_pattern(n_value_slots=4, n_label_slots=2)
    slide = SlideSpec(
        index=0, kind="kpi", headline="",
        blocks=[KpiBlock(items=[
            Kpi(value="-80%", label="сквозное время"),
            Kpi(value="-35 п.п.", label="доля вручную"),
            Kpi(value="-17 п.п.", label="доля с ошибкой"),
            Kpi(value="+1,3", label="удовлетворённость"),
        ])],
    )
    result = assign_content(slide, pattern, _grid())

    kpi_values = [c for c in result if c.role_hint == "kpi_value"]
    kpi_labels = [c for c in result if c.role_hint == "kpi_label"]
    # Ровно две метрики уместились ПАРОЙ (у раскладки только 2 kpi_label) —
    # ни одно значение не осталось без своей подписи.
    assert len(kpi_values) == 2
    assert len(kpi_labels) == 2
    assert {p.text for c in kpi_values for p in c.paragraphs} == {"-80%", "-35 п.п."}
    assert {p.text for c in kpi_labels for p in c.paragraphs} == {"сквозное время", "доля вручную"}

    # Оставшиеся две метрики (значение И подпись ВМЕСТЕ) ушли в фолбэк —
    # не потерялись и не разошлись по разным местам.
    fallback = [c for c in result if c.role_hint == "bullets"]
    assert len(fallback) == 1
    joined = "\n".join(p.text for p in fallback[0].paragraphs)
    assert "-17 п.п." in joined and "доля с ошибкой" in joined
    assert "+1,3" in joined and "удовлетворённость" in joined


def test_kpi_pairing_is_unaffected_when_labels_are_plentiful():
    """Регрессия: когда слотов `kpi_label` хватает на все метрики — прежнее
    поведение (все пары в свои слоты, фолбэка нет) не меняется."""
    pattern = _kpi_pattern(n_value_slots=4, n_label_slots=4)
    slide = SlideSpec(
        index=0, kind="kpi", headline="",
        blocks=[KpiBlock(items=[
            Kpi(value="-80%", label="сквозное время"),
            Kpi(value="-35 п.п.", label="доля вручную"),
        ])],
    )
    result = assign_content(slide, pattern, _grid())
    assert len([c for c in result if c.role_hint == "kpi_value"]) == 2
    assert len([c for c in result if c.role_hint == "kpi_label"]) == 2
    assert not [c for c in result if c.role_hint == "bullets"]
