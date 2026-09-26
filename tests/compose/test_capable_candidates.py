"""Задача V2, airy слайд 3: карточки, написанные под диаграмму Ганта,
уехали запасной раскладкой в «Паттерн + фото» с одним абзацем, и на
слайде осталась подпись «Май». Сборка теперь ставит вперёд кандидатов,
чьи возможности держат написанное, и не принимает клон, потерявший
главный блок целиком."""
from __future__ import annotations
from pathlib import Path

import pytest
from pptx import Presentation

from deckforge.audit.config import AuditConfig
from deckforge.compose import builder
from deckforge.compose.clone import sample_slides_by_number
from deckforge.pattern.forms import forms_of
from deckforge.plan.spec import Card, CardBlock, SlideSpec

TEMPLATE = Path("dataset/templates/Шаблон презентации VK Education.pptx")


@pytest.fixture(scope="module")
def profile(profile_fixture):
    return profile_fixture(TEMPLATE.name)


def _pattern(profile, pattern_id: str):
    return next(builder._pattern_from_model(m) for m in profile.patterns if m.pattern_id == pattern_id)


def _cards() -> SlideSpec:
    return SlideSpec(
        index=2, kind="image", headline="Ожидание сократится на 80%",
        blocks=[CardBlock(items=[Card(title="Май", body="Пилот в двух отделах"), Card(title="Дек", body="Раскатка")])],
    )


def test_clone_that_loses_the_cards_is_rejected(profile):
    prs = Presentation(str(TEMPLATE))
    sources = sample_slides_by_number(prs)
    builder._clear_sample_slides(prs)

    outcome = builder.place_slide_by_clone(prs, _cards(), _pattern(profile, "slide35"), profile, AuditConfig.load(), sources[35])

    assert outcome.reason is not None and "cards" in outcome.reason
    assert len(prs.slides) == 0


def test_candidates_that_hold_the_written_blocks_go_first(profile):
    candidates = [_pattern(profile, "slide35"), _pattern(profile, "slide21")]

    ordered = builder._capable_first(_cards(), candidates, forms_of(profile))

    assert [p.pattern_id for p in ordered] == ["slide21", "slide35"]
