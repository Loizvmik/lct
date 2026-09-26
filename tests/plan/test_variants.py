"""Тесты `plan.variants` после задачи Q: от модуля осталось перечисление
стилей. Выбор раскладок по стилю проверяют тесты `tests/pattern/`, а
разделители `airy` проверяет `tests/pattern/` через `intents_from_outline`."""
from __future__ import annotations

import pytest

from deckforge.pattern.style import STYLE_NAMES, load_style
from deckforge.plan.variants import GenerationStyle, Variant


def test_style_names_match_the_style_config():
    """Имя стиля в API и CLI должно находить свою строку в
    `config/styles.yaml`: иначе стиль молча получил бы запасные числа."""
    assert tuple(s.value for s in GenerationStyle) == STYLE_NAMES
    for style in GenerationStyle:
        assert load_style(style).name == style.value


def test_parse_accepts_names_and_members_and_rejects_unknown():
    assert GenerationStyle.parse("visual") is GenerationStyle.visual
    assert GenerationStyle.parse(GenerationStyle.airy) is GenerationStyle.airy
    with pytest.raises(ValueError, match="dense, airy, visual"):
        GenerationStyle.parse("bold")


def test_old_name_is_the_same_enum():
    """`compose.builder` и скрипты по-прежнему импортируют `Variant`."""
    assert Variant is GenerationStyle


def test_airy_style_keeps_section_dividers_in_config():
    """Разделители разделов остались свойством стиля `airy`, а не кода."""
    assert load_style("airy").dividers is True
    assert load_style("dense").dividers is False
