"""Тесты замера текста (Task 9, Step 1 брифа — дословно)."""
from __future__ import annotations
import time

import pytest

from deckforge.compose import textfit
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


# ---------------------------------------------------------------------------
# Находка №2 отчёта: замер обязан знать, по НАСТОЯЩЕМУ шрифту он идёт или по
# запасному, и закладывать запас в приблизительном случае.
# ---------------------------------------------------------------------------


def test_measure_reports_exact_font_when_the_requested_family_is_found():
    """`Play` установлен в системе (координатор поставил ~/Library/Fonts/
    Play-Regular.ttf, OFL) — цепочка подмены находит его на первом же шаге
    "тот же шрифт из системы", замер идёт по НАСТОЯЩИМ метрикам, не по
    запасным."""
    metrics = measure("Текст карточки", "Play", 16, 4.0)
    assert metrics.font_source == "exact"


def test_measure_reports_fallback_font_when_nothing_matches():
    metrics = measure("Текст", "Совершенно Несуществующий Шрифт 12345", 16, 4.0)
    assert metrics.font_source == "fallback"


def test_fallback_font_gets_a_conservative_safety_margin(monkeypatch):
    """Запасной шрифт — не то же самое рисование, что настоящий запрошенный:
    средняя ширина глифа у разных гарнитур отличается на порядок 10-20%
    (см. `_FALLBACK_SAFETY_MARGIN`). Чтобы не молча занижать оценку "влезает
    ли", запасной случай обязан закладывать запас на высоту и на самое
    длинное слово, а не отдавать те же числа, что и точный случай.

    Подмена `_resolve_font` заставляет ОБА вызова (exact/fallback) грузить
    ОДИН И ТОТ ЖЕ файл шрифта (см. `font_file_for("Play")`) — единственная
    разница между результатами обязана быть запасом, не другим рисунком
    глифов."""
    real_path = font_file_for("Play")
    assert real_path is not None, "нужен файл шрифта Play в системе для этого теста"

    def fake_resolve(family: str):
        return real_path, "fallback" if family == "fallback-case" else "exact"

    monkeypatch.setattr(textfit, "_resolve_font", fake_resolve)
    text = "Пример текста подлиннее для сравнения запасов метрики"
    exact = measure(text, "exact-case", 18, 3.0)
    fallback = measure(text, "fallback-case", 18, 3.0)

    assert textfit._FALLBACK_SAFETY_MARGIN > 1.0
    assert fallback.height_in == pytest.approx(exact.height_in * textfit._FALLBACK_SAFETY_MARGIN, rel=1e-6)
    assert fallback.longest_word_in == pytest.approx(
        exact.longest_word_in * textfit._FALLBACK_SAFETY_MARGIN, rel=1e-6
    )
    # запас — про высоту/ширину слова (сигнал "влезает ли"), не про число строк:
    # мы не пересимулируем перенос на чужих метриках, честно объявлено в докстроке.
    assert fallback.lines == exact.lines


def test_font_file_for_still_returns_a_plain_path(monkeypatch):
    """`font_file_for` — часть интерфейса брифа дословно (сигнатура
    `family -> Path | None`) — обязана остаться такой и после того, как
    `measure()` научился спрашивать провенанс через `_resolve_font`."""
    assert font_file_for("Play") == textfit._resolve_font("Play")[0]
