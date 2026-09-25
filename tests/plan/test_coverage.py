import pytest
import zipfile

from deckforge.plan.coverage import (
    ContentValidationError, extract_source_facts, source_coverage_report, validate_deck_content,
    validate_pptx_content,
)
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec


SOURCES = [
    "В пилоте участвовали 4 отдела и 410 заявок. Срок обработки сократился "
    "на 27%. Доля просроченных заявок снизилась с 23% до 6%."
]


def test_extracts_unique_numeric_source_facts_with_context():
    facts = extract_source_facts(SOURCES)
    assert [fact.normalized for fact in facts] == ["4", "410", "27%", "23%", "6%"]
    assert all(fact.context for fact in facts)


def test_reports_used_and_unused_source_facts():
    deck = DeckSpec(title="Пилот", language="ru", slides=[
        SlideSpec(
            index=0, kind="bullets", headline="Пилот охватил четыре отдела",
            blocks=[BulletBlock(items=["Обработано 410 заявок, срок сократился на 27%."])],
            source_note="Источник: материалы пилота",
        ),
    ])
    report = source_coverage_report(deck, SOURCES)
    assert report["used_fact_count"] == 2
    assert {item["value"] for item in report["facts"] if not item["used"]} == {"4", "23%", "6%"}
    assert all(item["reason"] for item in report["facts"] if not item["used"])


def test_rejects_generic_headline_only_fallback_deck():
    deck = DeckSpec(title="Пусто", language="ru", slides=[
        SlideSpec(index=index, kind="section", headline=headline, generation_origin="fallback")
        for index, headline in enumerate((
            "Тема и цель презентации", "Контекст задачи", "Что показывает измерение", "Итог и следующий шаг",
        ))
    ])
    with pytest.raises(ContentValidationError, match="содержательного текста"):
        validate_deck_content(deck, SOURCES)


def test_rejects_pptx_that_dropped_a_slide_block(tmp_path):
    deck = DeckSpec(title="Пилот", language="ru", slides=[
        SlideSpec(
            index=0, kind="bullets", headline="Итоги",
            blocks=[BulletBlock(items=["Обработано 410 заявок", "Срок сократился на 27%"])]),
    ])
    pptx = tmp_path / "missing-block.pptx"
    slide_xml = """<p:sld xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\"
        xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\">
        <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Итоги</a:t></a:r></a:p>
        <a:p><a:r><a:t>Обработано 410 заявок</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
        </p:sld>"""
    with zipfile.ZipFile(pptx, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide_xml)

    with pytest.raises(ContentValidationError, match="потеряны блоки"):
        validate_pptx_content(
            pptx, deck,
            {"facts": [{"value": "410", "used": True}]},
        )
