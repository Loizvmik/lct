"""Общая инфраструктура тестов Task 10 (`test_charts.py`/`test_tables.py`/
`test_diagrams.py`) — профиль строится на РЕАЛЬНЫХ файлах `dataset/
templates/` (не синтетика: графики/таблицы/схемы читают
`profile.chart_series`/`shape_vocabulary`/`type_scale`/`assets.icons`,
которые имеют смысл только на настоящем разборе), кешируется на весь
прогон (`functools.lru_cache`), тот же приём, что и `tests/template/
conftest.py`.
"""
from __future__ import annotations
import functools
from pathlib import Path

import pytest
from pptx import Presentation

from deckforge.ooxml.geometry import Box
from deckforge.template.profile import TemplateProfile

TEMPLATES_DIR = Path("dataset/templates")

# ЛЦТ2026 — контрольный шаблон, в тестах не участвует (тот же принцип, что
# и во всех остальных tests/*/conftest.py проекта).
DEFAULT_TEMPLATE = "VK Tech шаблон.pptx"


@functools.lru_cache(maxsize=None)
def _profile(name: str) -> TemplateProfile:
    return TemplateProfile.from_file(TEMPLATES_DIR / name, cache_dir=None)


@pytest.fixture(scope="session")
def profile_fixture():
    """`profile_fixture(name) -> TemplateProfile` — вызываемая фикстура,
    как в `tests/template/conftest.py`."""
    return _profile


@pytest.fixture(scope="session")
def PROFILE(profile_fixture) -> TemplateProfile:
    return profile_fixture(DEFAULT_TEMPLATE)


@pytest.fixture
def new_slide():
    """`new_slide()` — чистый слайд на ПУСТОМ шаблоне `python-pptx` (не на
    файле шаблона DeckForge): графикам/таблицам/схемам из `TemplateProfile`
    нужны только данные профиля (палитра/шкала/словарь форм), не реальные
    лейауты конкретного .pptx — развязка позволяет тестам не зависеть от
    того, что несёт лейаут №6 в каждом из четырёх файлов датасета."""
    def _make():
        prs = Presentation()
        return prs.slides.add_slide(prs.slide_layouts[6])
    return _make


@pytest.fixture
def BOX() -> Box:
    return Box(left=0.1, top=0.15, width=0.6, height=0.5)


@pytest.fixture
def SMALL_BOX() -> Box:
    return Box(left=0.1, top=0.15, width=0.2, height=0.12)
