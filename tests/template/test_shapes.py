"""Тесты `template/shapes.py` — словарь карточных форм (Task 10 + вторая
правка код-ревью, см. докстроку модуля).

Первая правка (`_card_decor_vocabulary` по декору групп повтора вместо
переписи всех автофигур пакета) уже покрыта параметризованным тестом
`tests/compose/test_diagrams.py::test_diagram_card_uses_the_templates_
dominant_shape` — он сверяет `profile.shape_vocabulary[0].prst` с реальным
замером на всех трёх учебных шаблонах и гоняет реальную сборку схемы этой
формой, поэтому здесь не дублируется.

Этот файл — вторая правка код-ревью: декор группы повтора без текста внутри
(круглый аватар/бейдж под иконку) не должен попадать в карточный словарь,
даже если шагает той же сеткой, что карточки контента. Синтетика — прямые
`Pattern`/`DecorShape`/`PatternSlot`, без разбора реального `.pptx`, тот же
приём "белого ящика", что и остальной `tests/template/test_patterns.py`
(`_to_decor`/`_decor_repeat_membership`/`_classify_kind` тестируются там
напрямую, не только через `profile_fixture`)."""
from __future__ import annotations

from pathlib import Path

from deckforge.ooxml.geometry import Box
from deckforge.ooxml.package import PptxPackage
from deckforge.template.patterns import Capacity, DecorShape, Pattern, PatternSlot
from deckforge.template.shapes import (
    _card_decor_vocabulary,
    _contains_text,
    _slide_vocabulary,
    build_shape_vocabulary,
)

TEMPLATES_DIR = Path("dataset/templates")

_CAPACITY = Capacity(max_items=4, max_chars_per_item=80, max_bullets=0, max_series=0, max_rows=0, max_cols=0)


def _text_slot(box: Box, role: str = "card_body") -> PatternSlot:
    return PatternSlot(
        role=role, box=box, size_pt=12.0, color_hex="#000000", align="left",
        max_chars=80, wraps=True, sample_text="Текст карточки",
    )


def _icon_slot(box: Box) -> PatternSlot:
    return PatternSlot(
        role="icon", box=box, size_pt=0.0, color_hex=None, align="center",
        max_chars=0, wraps=False, sample_text=None,
    )


def _decor(box: Box, prst: str, *, repeat_group: bool = True) -> DecorShape:
    return DecorShape(
        kind="shape", box=box, rotation=0.0, flip_h=False, flip_v=False,
        fill_hex="#FFFFFF", has_fill=True, repeat_group=repeat_group, repeat_index=0, prst=prst,
    )


def _pattern(slots: list[PatternSlot], decor: list[DecorShape]) -> Pattern:
    return Pattern(
        pattern_id="synthetic", source_slide_index=[0], layout_id="L1", kind="cards",
        slots=slots, repeat=None, decor=decor, capacity=_CAPACITY, score=1.0, is_dark=False,
    )


def test_card_vocabulary_prefers_the_rect_with_text_over_the_round_icon_badge():
    """Синтетика брифа: раскладка с круглым бейджем под иконку (текста
    внутри нет — `icon`-слот не текстовая роль, и он вне бейджа) и
    прямоугольной плашкой, целиком обрамляющей текстовый слот. В словарь
    обязан попасть `rect`, а не `ellipse`."""
    icon_badge = _decor(Box(0.10, 0.10, 0.05, 0.05), "ellipse")
    icon = _icon_slot(Box(0.11, 0.11, 0.03, 0.03))

    text_card = _decor(Box(0.10, 0.20, 0.30, 0.15), "rect")
    text_slot = _text_slot(Box(0.12, 0.21, 0.26, 0.13))

    pattern = _pattern([icon, text_slot], [icon_badge, text_card])

    entries = _card_decor_vocabulary([pattern])
    assert entries, "карточный декор обязан найтись"
    assert entries[0].prst == "rect", [(e.prst, e.count) for e in entries]
    assert not any(e.prst == "ellipse" for e in entries), (
        "бейдж без текста внутри не должен попасть в словарь вовсе",
        [(e.prst, e.count) for e in entries],
    )


def test_contains_text_ignores_a_bullet_dot_barely_touching_the_slot():
    """Второе сомнение отчёта Task 10 код-ревью: мелкая фигура (буллет-
    точка), которая лишь слегка задевает угол текстового слота, не должна
    засчитываться как обрамляющая текст — доля площади слота внутри неё
    далека от порога `_TEXT_CONTAINMENT_THRESHOLD`."""
    bullet_dot = _decor(Box(0.10, 0.10, 0.01, 0.01), "ellipse")
    text_slot = _text_slot(Box(0.105, 0.105, 0.30, 0.05))  # точка лишь чуть перекрывает угол слота

    assert not _contains_text(bullet_dot, [text_slot])


def test_contains_text_tolerates_a_slot_sticking_out_a_couple_of_percent():
    """Обратный случай: слот на пару процентов площади выступает за плашку
    (типографский инсет) — это нормально, декор всё ещё засчитывается
    карточным (брифом задачи прямо оговорено)."""
    card = _decor(Box(0.10, 0.20, 0.30, 0.15), "roundRect")
    # Слот шире плашки на ~3% по низу (0.34 -> 0.355 высоты) — почти всё
    # внутри, наружу торчит небольшой остаток.
    slot = _text_slot(Box(0.11, 0.21, 0.28, 0.145))

    assert _contains_text(card, [slot])


def test_card_vocabulary_falls_back_to_slide_wide_count_without_text_bearing_decor():
    """Регрессия на запасной путь (докстрока модуля): если ни один декор
    группы повтора не обрамляет текст (здесь — только бейдж без текста
    внутри), `build_shape_vocabulary` не возвращает пустой список, а
    считает автофигуры по слайдам (`_slide_vocabulary`), как и раньше."""
    icon_badge = _decor(Box(0.10, 0.10, 0.05, 0.05), "ellipse")
    icon = _icon_slot(Box(0.11, 0.11, 0.03, 0.03))
    pattern = _pattern([icon], [icon_badge])

    assert _card_decor_vocabulary([pattern]) == []

    with PptxPackage.open(TEMPLATES_DIR / "VK Tech шаблон.pptx") as pkg:
        canvas = pkg.canvas()
        expected = _slide_vocabulary(pkg, canvas)
        result = build_shape_vocabulary(pkg, canvas, [pattern])

    assert result, "запасной путь обязан дать непустой словарь"
    assert result == expected
