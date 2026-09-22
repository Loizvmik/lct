"""Инфраструктура тестов `test_soffice.py` — независимая копия небольшого
`DeckSpec`/сборки на VK Tech (тот же принцип "тесты не делят приватные
фикстуры между модулями", что и в остальном проекте, см. `tests/compose/
conftest.py`). Рендер PDF/PNG не зависит от того, есть ли в шаблоне
паттерны table/chart/kpi (в отличие от `tests/export/`, где это важно) —
здесь достаточно маленькой, быстрой в сборке колоды."""
from __future__ import annotations
import functools
from pathlib import Path

import pytest

from deckforge.compose.builder import Variant, build_deck
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec, ensure_valid_deck_spec
from deckforge.template.profile import TemplateProfile

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")


@functools.lru_cache(maxsize=None)
def _profile() -> TemplateProfile:
    return TemplateProfile.from_file(TEMPLATE, cache_dir=None)


@pytest.fixture(scope="session")
def PROFILE() -> TemplateProfile:
    return _profile()


def _deck_spec() -> DeckSpec:
    return DeckSpec(
        title="Рендер: проверка PDF/PNG",
        language="ru",
        slides=[
            SlideSpec(index=0, kind="section", headline="Рендер PDF/PNG"),
            SlideSpec(
                index=1, kind="bullets", headline="Что проверяем",
                blocks=[BulletBlock(items=["Число страниц PDF", "Разрешение PNG", "Параллельные вызовы"])],
            ),
        ],
    )


@functools.lru_cache(maxsize=None)
def _built() -> tuple[DeckSpec, Path]:
    deck = _deck_spec()
    ensure_valid_deck_spec(deck)
    pptx_path = build_deck(deck, _profile(), TEMPLATE, Variant.dense)
    return deck, pptx_path


@pytest.fixture(scope="session")
def DECK() -> DeckSpec:
    return _built()[0]


@pytest.fixture(scope="session")
def PPTX() -> Path:
    return _built()[1]
