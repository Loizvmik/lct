"""Общая инфраструктура тестов `tests/plan/` — тот же приём, что и
`tests/compose/conftest.py`/`tests/template/conftest.py`: профиль строится
на РЕАЛЬНОМ файле `dataset/templates/` (не синтетика — `writer`/`variants`
читают `profile.patterns`/`.capacity` целиком, которые имеют смысл только
на настоящем разборе), один раз на всю сессию тестов."""
from __future__ import annotations
from pathlib import Path

import pytest

from deckforge.template.profile import TemplateProfile

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")


@pytest.fixture(scope="session")
def PROFILE() -> TemplateProfile:
    return TemplateProfile.from_file(TEMPLATE, cache_dir=None)


@pytest.fixture(scope="session")
def TEMPLATE_PATH() -> Path:
    return TEMPLATE
