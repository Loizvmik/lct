"""Тесты `plan.spec.deck_spec_to_dict`/`deck_spec_from_debug_dict` — пара
округляющая (round-trip) дамп/загрузку СОБСТВЕННОГО `DeckSpec` через диск,
отдельная от `deck_spec_from_dict` (та — граница с моделью, см. докстроку в
`spec.py`). Нужна задаче "аудит по картинке — отдельная команда": `deckforge
audit-visual` запускается на уже готовом `.pptx` и должна прочитать обратно
`DeckSpec`, который `deckforge generate` дампит для отладки."""
from __future__ import annotations

import pytest

from deckforge.plan.spec import (
    BulletBlock, Card, CardBlock, ChartSeriesData, ChartVisual, DeckSpec, Kpi, KpiBlock,
    QuoteBlock, SlideSpec, SpecValidationError, TableVisual, TextBlock, Visual,
    deck_spec_from_debug_dict, deck_spec_to_dict, visual_from_dict,
)


def _rich_deck() -> DeckSpec:
    return DeckSpec(
        title="Колода", language="ru",
        meta={"source": "test"},
        slides=[
            SlideSpec(
                index=0, kind="bullets", headline="Заголовок 1", subhead="Подзаголовок",
                blocks=[TextBlock(text="Абзац"), BulletBlock(items=["раз", "два"])],
                source_note="Источник: тест", speaker_notes="Заметки докладчика",
                findings=["найдено что-то"], pattern_id="slide7",
            ),
            SlideSpec(
                index=1, kind="cards", headline="Заголовок 2",
                blocks=[CardBlock(items=[Card(title="A", body="Тело A"), Card(body="Тело B")])],
            ),
            SlideSpec(
                index=2, kind="kpi", headline="Заголовок 3",
                blocks=[KpiBlock(items=[Kpi(value="80%", label="доля")])],
            ),
            SlideSpec(
                index=3, kind="section", headline="Цитата",
                blocks=[QuoteBlock(text="Слова", author="Кто-то")],
            ),
            SlideSpec(
                index=4, kind="table", headline="Таблица и график",
                visual=Visual(
                    kind="chart", caption="Подпись визуала",
                    table=TableVisual(rows=[["A", "B"], ["1", "2"]]),
                    chart=ChartVisual(
                        kind="bar", categories=["янв", "фев"],
                        series=[ChartSeriesData(name="Ряд", values=[1.0, 2.5])],
                        unit="шт", highlight_index=1, axis_titles=("Месяц", "Штуки"),
                    ),
                ),
            ),
        ],
    )


def test_deck_spec_round_trips_through_dict():
    original = _rich_deck()
    restored = deck_spec_from_debug_dict(deck_spec_to_dict(original))

    assert restored.title == original.title
    assert restored.language == original.language
    assert restored.meta == original.meta
    assert len(restored.slides) == len(original.slides)

    for before, after in zip(original.slides, restored.slides):
        assert after.index == before.index
        assert after.kind == before.kind
        assert after.headline == before.headline
        assert after.subhead == before.subhead
        assert after.blocks == before.blocks
        assert after.visual == before.visual
        assert after.source_note == before.source_note
        assert after.speaker_notes == before.speaker_notes
        assert after.findings == before.findings
        assert after.pattern_id == before.pattern_id


def test_deck_spec_to_dict_tags_every_block_with_a_type():
    data = deck_spec_to_dict(_rich_deck())
    for slide in data["slides"]:
        for block in slide["blocks"]:
            assert "type" in block, "без тега type text/quote и bullets/cards/kpi неразличимы при загрузке"


def test_deck_spec_round_trip_handles_empty_deck():
    empty = DeckSpec(title="Пусто", language="ru", slides=[])
    restored = deck_spec_from_debug_dict(deck_spec_to_dict(empty))
    assert restored.slides == []


# ---------------------------------------------------------------------------
# Модель кладёт таблицу на уровень выше — живой прогон 25 сентября 2026,
# слайд ушёл в запасной вариант с ошибкой «неизвестные поля ['rows']»
# ---------------------------------------------------------------------------


def test_a_flattened_table_is_lifted_into_place():
    """Схема с двойной вложенностью — наше внутреннее устройство, и модель
    на нём спотыкается. Терять готовый слайд из-за уровня вложенности
    дороже, чем поднять поля кодом: намерение однозначно."""
    visual = visual_from_dict({"kind": "table", "rows": [["Этап", "Часы"], ["Ожидание", "18"]]}, "x")

    assert visual is not None and visual.table is not None
    assert visual.table.rows[0] == ["Этап", "Часы"]


def test_a_properly_nested_table_is_left_alone():
    """Присланный `table` всегда главнее — смешивать его с плоскими полями
    значило бы уже угадывать."""
    visual = visual_from_dict({"kind": "table", "table": {"rows": [["a"]]}}, "x")

    assert visual is not None and visual.table is not None
    assert visual.table.rows == [["a"]]


def test_a_flattened_chart_is_not_guessed():
    """У графика есть СВОЙ вид (столбики/линия/круг), и в плоском виде его
    негде взять. Подставлять его за модель — решать за неё, как показать
    данные."""
    with pytest.raises(SpecValidationError):
        visual_from_dict({"kind": "chart", "categories": ["a"], "series": []}, "x")
