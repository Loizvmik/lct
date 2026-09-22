"""Тесты `plan.factcheck.check_number_in_sources` — инструмент №2 агентного
цикла `plan.writer` (Task 19): даёт модели способ свериться самой, что
число/факт действительно встречается в исходных материалах слайда, вместо
того чтобы полагаться только на запрет промптом (проверяемый постфактум,
визуальным аудитом, и только если его вообще запустят)."""
from __future__ import annotations

from deckforge.plan.factcheck import check_number_in_sources


SOURCES = (
    "### pilot.md\n"
    "Сквозная медиана обработки заявки — 6,2 часа. 98,5% времени заявка "
    "находится в ожидании, а не в активной обработке. Выборка нетипична: "
    "только заявки одного региона.\n"
)


def test_finds_a_number_written_the_same_way():
    result = check_number_in_sources("6,2", SOURCES)
    assert result["found"] is True
    assert "6,2 часа" in result["context"]


def test_finds_a_number_with_a_different_decimal_separator():
    """Модель могла написать "98.5%" (точка), источник несёт "98,5%"
    (запятая) — то же число, должно найтись."""
    result = check_number_in_sources("98.5%", SOURCES)
    assert result["found"] is True
    assert result["occurrences"] >= 1


def test_number_absent_from_sources_is_reported_as_not_found():
    result = check_number_in_sources("42%", SOURCES)
    assert result["found"] is False
    assert result["context"] is None
    assert result["occurrences"] == 0


def test_finds_a_short_phrase_not_only_numbers():
    result = check_number_in_sources("нетипична", SOURCES)
    assert result["found"] is True
    assert "выборка" in result["context"].lower()


def test_empty_query_is_reported_as_not_found_not_a_false_match():
    """Пустая строка находится в любом тексте буквально — `found=True` был
    бы бесполезной ложной уверенностью, не честной деградацией."""
    result = check_number_in_sources("", SOURCES)
    assert result["found"] is False


def test_empty_sources_do_not_raise():
    result = check_number_in_sources("6,2", "")
    assert result["found"] is False
