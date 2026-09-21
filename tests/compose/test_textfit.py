"""Тесты замера текста (Task 9, Step 1 брифа — дословно)."""
from __future__ import annotations
import time

from deckforge.compose.textfit import font_file_for, measure


def test_longer_text_wraps_into_more_lines():
    short = measure("Короткий", "Arial", 16, 4.0)
    long = measure("Очень длинный текст, который точно не поместится в одну строку", "Arial", 16, 4.0)
    assert long.lines > short.lines


def test_narrower_box_gives_more_lines():
    text = "Узкое место процесса — не работа, а ожидание между этапами"
    assert measure(text, "Arial", 16, 2.0).lines > measure(text, "Arial", 16, 6.0).lines


def test_height_grows_with_size():
    assert measure("Заголовок", "Arial", 32, 6.0).height_in > measure("Заголовок", "Arial", 16, 6.0).height_in


def test_soft_break_counts_as_a_line():
    assert measure("Первая\x0bвторая", "Arial", 16, 8.0).lines == 2


def test_unknown_font_falls_back_without_raising():
    metrics = measure("Текст", "Совершенно Несуществующий Шрифт", 16, 4.0)
    assert metrics.lines >= 1


def test_measurement_is_cached():
    """Без кэша слайд с таблицей 11×6 мерялся минутами."""
    text = "ячейка " * 20
    measure(text, "Arial", 12, 1.0)
    start = time.monotonic()
    for _ in range(500):
        measure(text, "Arial", 12, 1.0)
    assert time.monotonic() - start < 0.5


def test_font_file_for_finds_arial_or_falls_back_without_raising():
    """`font_file_for` — публичная часть интерфейса (брифом, импорт в тесте
    Step 1 дословно: `from deckforge.compose.textfit import measure,
    font_file_for`) — сама по себе не должна падать ни на реальном
    семействе, ни на несуществующем."""
    assert font_file_for("Arial") is None or font_file_for("Arial").is_file()
    assert font_file_for("Совершенно Несуществующий Шрифт 12345") is None or True


def test_longest_word_in_grows_with_size():
    short = measure("Слово", "Arial", 16, 4.0)
    long = measure("Слово", "Arial", 32, 4.0)
    assert long.longest_word_in > short.longest_word_in
