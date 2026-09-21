"""Майнинг композиционных паттернов со слайдов-примеров — тесты дословно из
брифа Task 7 (Step 1), .superpowers/sdd/task-7-brief.md.

Как и в test_typography.py/test_grid.py/test_layouts.py/test_assets.py,
`profile_fixture` в телах тестов брифа нужен как параметр — иначе pytest не
подставит фикстуру и вызов упадёт с NameError; это единственная правка
против буквального текста брифа (тот же приём уже применялся в Task 3-6).
"""
from __future__ import annotations
from conftest import ALL_TEMPLATES


def test_patterns_are_mined_from_every_template(profile_fixture):
    for name in ALL_TEMPLATES:
        patterns = profile_fixture(name).patterns
        assert len(patterns) >= 8, f"{name}: слишком мало паттернов, генератору не из чего выбирать"


def test_card_grid_is_detected_in_vk_tech(profile_fixture):
    """1553 автошейпа, 1502 с noFill — это карточные композиции."""
    patterns = profile_fixture("VK Tech шаблон.pptx").patterns
    cards = [p for p in patterns if p.kind == "cards"]
    assert cards
    assert any(p.repeat and p.repeat.count >= 3 for p in cards)


def test_two_column_pattern_has_symmetric_slots(profile_fixture):
    patterns = profile_fixture("Шаблон презентации VK Education.pptx").patterns
    two_col = [p for p in patterns if p.kind == "two_col"]
    assert two_col
    left, right = sorted(two_col[0].slots, key=lambda s: s.box.left)[:2]
    assert abs(left.box.width - right.box.width) < 0.02


def test_every_pattern_has_a_headline_slot_or_is_marked_decorative(profile_fixture):
    for name in ALL_TEMPLATES:
        for pattern in profile_fixture(name).patterns:
            roles = {slot.role for slot in pattern.slots}
            assert "headline" in roles or pattern.kind in {"section", "image", "closing"}


def test_slots_respect_template_margins(profile_fixture):
    for name in ALL_TEMPLATES:
        grid = profile_fixture(name).grid
        for pattern in profile_fixture(name).patterns:
            for slot in pattern.slots:
                assert slot.box.left >= grid.margin_left - 0.01
                assert slot.box.right <= 1 - grid.margin_right + 0.01


def test_capacity_is_derived_from_measured_box_not_guessed(profile_fixture):
    """max_chars слота должен считаться замером текста в его рамке,
    иначе генератор напишет текст, который не влезет."""
    pattern = profile_fixture("VK Tech шаблон.pptx").patterns[0]
    headline = next(s for s in pattern.slots if s.role == "headline")
    assert 20 <= headline.max_chars <= 300


def test_soft_line_break_does_not_corrupt_slot_text(profile_fixture):
    """В шаблонах перенос строки — a:br, который text_frame отдаёт как \\x0b."""
    for name in ALL_TEMPLATES:
        for pattern in profile_fixture(name).patterns:
            for slot in pattern.slots:
                assert "\x0b" not in (slot.sample_text or "")
